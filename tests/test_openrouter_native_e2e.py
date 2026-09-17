"""Opt-in real CLI tests; all inference goes to a local deterministic provider.

Set SCOREBENCH_TEST_OPENCODE_BIN and/or SCOREBENCH_TEST_PI_BIN to installed
executables. No real provider credentials, account, or paid calls are used.
"""
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/scorebench/scripts"
MODEL = "scorebench/probe"


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        model = getattr(self.server, "model", MODEL)
        if self.path == "/api/v1/models":
            body = json.dumps({"data": getattr(self.server, "model_catalog", [])}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if urlsplit(self.path).path == "/api/v1/generation":
            generation_id = parse_qs(urlsplit(self.path).query).get("id", [""])[0]
            self.server.generation_reads.append(generation_id)
            data = self.server.generations.get(generation_id)
            if not data:
                self.send_error(404)
                return
            body = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != f"/api/v1/models/{model}/endpoints":
            self.send_error(404)
            return
        body = json.dumps({"data": {"id": model, "name": "Probe", "endpoints": [{
            "supported_parameters": ["tools", "reasoning"], "context_length": getattr(self.server, "context_limit", 32768),
            "max_completion_tokens": getattr(self.server, "output_limit", 1000), "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        }]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.requests.append((self.path, self.headers.get("Authorization"), request))
            seq = len(self.server.requests)
        failure = getattr(self.server, "http_failures", {}).get(seq)
        if failure:
            body = b"Bad Gateway" if failure == 502 else b"Temporary upstream failure"
            self.send_response(failure)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(body)
            return
        if seq in getattr(self.server, "drop_before_headers", set()):
            # Acceptance is deliberately ambiguous: request arrived, receipt did not.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        messages = request.get("messages", [])
        tools = request.get("tools", [])
        had_tool = any(message.get("role") == "tool" for message in messages)
        tool = next((t["function"]["name"] for t in tools if t["function"]["name"] == "write"), None)
        if not tool:
            tool = next((t["function"]["name"] for t in tools if t["function"]["name"] == "apply_patch"), None)
        call_tool = tool and not had_tool
        delta = {"role": "assistant", "content": "ScoreBench probe complete."}
        if call_tool:
            props = next(t["function"]["parameters"]["properties"] for t in tools if t["function"]["name"] == tool)
            arguments = {"filePath" if "filePath" in props else "path": str(self.server.workspace / "probe.txt"),
                         "content": "scorebench-probe\n"}
            if tool == "apply_patch":
                arguments = {"patchText": "*** Begin Patch\n*** Add File: probe.txt\n+scorebench-probe\n*** End Patch"}
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call-{seq}", "type": "function",
                     "function": {"name": tool, "arguments": json.dumps(arguments)}}]}
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                 "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10},
                 "completion_tokens_details": {"reasoning_tokens": 5}, "cost": getattr(self.server, "unit_cost", 0.002)}
        reason = "tool_calls" if call_tool else "stop"
        if seq in getattr(self.server, "length_steps", set()):
            reason = "length"
            delta = {"role": "assistant", "reasoning": "Long reasoning was truncated."}
        override = getattr(self.server, "response_steps", {}).get(seq, {})
        delta = override.get("delta", delta)
        reason = override.get("reason", reason)
        usage.update(override.get("usage", {}))
        envelope = {"id": f"gen-{seq}", "object": "chat.completion.chunk", "created": 1, "model": request["model"]}
        events = [
            {**envelope, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**envelope, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
            {**envelope, "choices": [], "usage": usage},
        ]
        if seq in getattr(self.server, "omit_final_usage", set()):
            events = events[:-1]
        body = ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if seq in getattr(self.server, "drop_after_usage", set()):
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NativeE2ETests(unittest.TestCase):
    def run_agent(self, kind, binary, model=MODEL, effort="low", length_stop=False, retained_recovery=False, receipt_lookup=False, early_stop=False, compaction=False, ip_family=None, delayed_receipt=False, gateway_failures=False):
        with tempfile.TemporaryDirectory(prefix="scorebench-native-") as root:
            root = Path(root)
            home, work, bin_dir = root / "home", root / "work", root / "bin"
            for path in (home, work, bin_dir):
                path.mkdir()
            cli = bin_dir / "scorebench"
            cli.write_text(f"#!{sys.executable}\n" + '''import json,sys
from pathlib import Path
if sys.argv[1:3] == ['run','usage']:
    with Path('snapshots.jsonl').open('a') as f: f.write(json.dumps(sys.argv[3:])+'\\n')
    print('{"ok":true}')
elif sys.argv[1:3] == ['run','progress']:
    if Path('assignment.json').exists():
        snapshots=[json.loads(line) for line in Path('snapshots.jsonl').read_text().splitlines()]
        flags=snapshots[-1]; used=float(flags[flags.index('--cost-usd')+1])
        print(json.dumps({'scope':{'kind':'run_token'}, 'run':{'run_id':'probe-run','status':'active'},
          'progress':{'budget':{'available':True,'type':'cost','target':3.0,'used':used,'remaining':max(0,3-used),'reached':used>=3}}}))
    else: print(json.dumps({'progress':{'budget':{'reached':False,'type':'none'},
                          **({'openrouter_failure_policy':'openrouter-infrastructure-v1'} if Path('experiment-policy').exists() else {})}}))
elif sys.argv[1:3] == ['run','current']:
    print(Path('assignment.json').read_text())
elif sys.argv[1:3] == ['run','gate']:
    print('{"ready":true}')
elif sys.argv[1:3] == ['run','ping']:
    with Path('pings.jsonl').open('a') as f: f.write(json.dumps(sys.argv[3:])+'\\n')
    if 'finish' in sys.argv and Path('assignment.json').exists():
        flags=json.loads(Path('snapshots.jsonl').read_text().splitlines()[-1])
        if float(flags[flags.index('--cost-usd')+1]) < 2.85:
            print('HTTP 400: budget not reached',file=sys.stderr); raise SystemExit(1)
    print('{"ok":true}')
else:
    raise SystemExit(2)
''')
            cli.chmod(0o755)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
            server.lock, server.requests, server.workspace = threading.Lock(), [], work
            server.model = model
            if gateway_failures:
                server.http_failures = {1: 502, 2: 503, 4: 429}
                (work / "experiment-policy").touch()
            if receipt_lookup:
                server.omit_final_usage = {2}
                server.generation_reads = []
                server.model_catalog = [{"id": model, "canonical_slug": model + "-20260910"}]
                server.generations = {"gen-2": {"id": "gen-2", "model": model + "-20260910",
                    "native_tokens_prompt": 100, "native_tokens_completion": 20,
                    "native_tokens_cached": 40, "native_tokens_reasoning": 5,
                    "total_cost": 0.002, "finish_reason": "stop"}}
                if delayed_receipt:
                    server.generations["gen-2"].update(total_cost=1.0, finish_reason=None)
            if length_stop or early_stop:
                server.length_steps = {2} if length_stop else set()
                if early_stop:
                    server.response_steps = {2: {"delta": {"role": "assistant", "content": ""}, "reason": "stop"}}
                server.unit_cost = 1.0
                server.output_limit = 131072
                server.context_limit = 500000
                if compaction:
                    server.unit_cost = 0.75
                    server.output_limit = 1000
                    server.context_limit = 32768
                    server.response_steps = {
                        1: {"usage": {"prompt_tokens": 40000, "total_tokens": 40020}},
                        2: {"delta": {"role": "assistant", "content": "Summary: probe.txt was written. Further validation remains."}, "reason": "stop"},
                        3: {"delta": {"role": "assistant", "content": ""}, "reason": "stop"},
                        4: {"delta": {"role": "assistant", "content": "Continued the original task."}, "reason": "stop"},
                    }
                assignment = {"run": {"run_id": "probe-run", "metadata": {
                    "coding_harness": "OpenCode", "model": "openrouter/" + model,
                    "effort": effort, "completion_policy": "budget_or_target"}}}
                (work / "assignment.json").write_text(json.dumps(assignment))
                (work / ".scorebench").mkdir()
                (work / ".scorebench/supervisor-run-start.json").write_text(json.dumps(assignment))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            env = {"HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}",
                   "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
                   "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local/state"),
                   "OPENROUTER_API_KEY": "fake-native-test-key",
                   "OPENROUTER_BASE": f"http://127.0.0.1:{server.server_port}",
                   "PI_OFFLINE": "1", "PI_TELEMETRY": "0", "OPENCODE_DISABLE_MODELS_FETCH": "true"}
            if ip_family is not None:
                env["SCOREBENCH_OPENROUTER_IP_FAMILY"] = ip_family
            expected_family = "4" if ip_family is None else ip_family
            prompt = "Use the write tool to create probe.txt containing scorebench-probe, then say done."
            if kind == "Pi":
                command = [binary, "--provider", "openrouter", "--model", model,
                           "--thinking", effort, "--offline", "--no-extensions", "--no-skills", "--no-context-files",
                           "--mode", "json", "--print", prompt]
            else:
                command = [binary, "run", "--pure", "--auto", "--model", "openrouter/" + model,
                           "--variant", effort, "--title", "ScoreBench local probe", "--format", "json", prompt]
            try:
                launcher = [sys.executable, str(SCRIPTS / "openrouter_run.py"),
                    "--harness", kind, "--workspace", str(work), "--expected-model", model, "--expected-effort", effort,
                    "--runtime-control"]
                result = subprocess.run([*launcher, *(["--no-auto-reentry"] if retained_recovery else []), "--", *command], env=env, cwd=work,
                    capture_output=True, text=True, timeout=90)
                if retained_recovery:
                    self.assertEqual(result.returncode, 1, result.stderr[-2000:])
                    accounting = work / ".scorebench/openrouter"
                    baseline = (accounting / "token-state.json").read_bytes()
                    prefix = (accounting / "usage.jsonl").read_bytes()
                    events = [json.loads(line) for line in (accounting / "native-output.jsonl").read_text().splitlines()]
                    session = events[-1]["sessionID"]
                    recovery = [*launcher, "--recover-session", session]
                    if delayed_receipt:
                        failed = json.loads((accounting / "result.json").read_text())
                        self.assertFalse(failed["accounting_ok"])
                        self.assertEqual(len(server.requests), 2)
                        server.generations["gen-2"]["finish_reason"] = "stop"
                        reconcile = [sys.executable, str(SCRIPTS / "openrouter_reconcile.py"), "--workspace", str(work)]
                        deadline = time.monotonic() + 65
                        while True:
                            check = subprocess.run([*reconcile, "--check"], env=env, capture_output=True, text=True, timeout=30)
                            self.assertEqual((accounting / "usage.jsonl").read_bytes(), prefix)
                            if check.returncode == 0:
                                break
                            self.assertLess(time.monotonic(), deadline, check.stderr + check.stdout)
                            time.sleep(1)
                        repaired = subprocess.run(reconcile, env=env, capture_output=True, text=True, timeout=30)
                        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
                        self.assertEqual(len(server.requests), 2)
                        prefix = (accounting / "usage.jsonl").read_bytes()
                        self.assertEqual((accounting / "token-state.json").read_bytes(), baseline)
                    preview = subprocess.run([*recovery, "--check", "--", *command], env=env, cwd=work,
                                             capture_output=True, text=True, timeout=45)
                    self.assertEqual(preview.returncode, 0, preview.stderr)
                    self.assertTrue(json.loads(preview.stdout)["recoverable"])
                    self.assertEqual((accounting / "usage.jsonl").read_bytes(), prefix)
                    self.assertEqual(len(server.requests), 2)
                    self.assertFalse((accounting / "reentries.json").exists())
                    result = subprocess.run([*recovery, "--", *command], env=env, cwd=work,
                                            capture_output=True, text=True, timeout=90)
                    self.assertEqual((accounting / "token-state.json").read_bytes(), baseline)
                    self.assertTrue((accounting / "usage.jsonl").read_bytes().startswith(prefix))
                self.assertEqual(result.returncode, 0, result.stderr[-5000:] + result.stdout[-3000:])
                self.assertTrue((work / "probe.txt").exists(), result.stderr[-2000:] + result.stdout[-4000:])
                self.assertEqual((work / "probe.txt").read_text(), "scorebench-probe\n")
                private_sessions = work / ".scorebench/openrouter" / ("pi-agent" if kind == "Pi" else "opencode-data")
                self.assertTrue(private_sessions.is_dir())
                self.assertGreaterEqual(len(server.requests), 2)
                self.assertLessEqual(len(server.requests), 6)
                for path, auth, request in server.requests:
                    self.assertEqual(path, "/api/v1/chat/completions")
                    self.assertEqual(auth, "Bearer fake-native-test-key")
                    self.assertEqual(request["model"], model)
                    self.assertEqual(request.get("reasoning", {}).get("effort"), effort)
                    if length_stop or early_stop:
                        self.assertEqual(request.get("max_tokens"), 1000 if compaction else 128000)
                ledger = [json.loads(line) for line in (work / ".scorebench/openrouter/usage.jsonl").read_text().splitlines()]
                self.assertEqual(len(ledger), len(server.requests) + int(delayed_receipt) + int(gateway_failures))
                snapshots = [json.loads(line) for line in (work / "snapshots.jsonl").read_text().splitlines()]
                first, last = snapshots[0], snapshots[-1]
                self.assertEqual(first[first.index("--total-tokens") + 1], "0")
                count = len(server.requests) - (3 if gateway_failures else 0)
                extra_input = 39900 if compaction else 0
                for flag, expected in (("--total-tokens", 80 * count + extra_input), ("--input-tokens", 50 * count + extra_input + (10 if receipt_lookup else 0)),
                                       ("--output-tokens", 20 * count), ("--cache-read-tokens", 40 * count),
                                       ("--cache-creation-tokens", 10 * count - (10 if receipt_lookup else 0)),
                                       ("--cost-usd", (0.75 if compaction else 1.0 if length_stop or early_stop else 0.002) * count)):
                    self.assertAlmostEqual(float(last[last.index(flag) + 1]), expected)
                self.assertNotIn("fake-native-test-key", (work / ".scorebench/openrouter/usage.jsonl").read_text())
                self.assertTrue(json.loads((work / ".scorebench/openrouter/result.json").read_text())["completion_confirmed"])
                self.assertEqual(json.loads((work / ".scorebench/openrouter/result.json").read_text())["transport"]["ip_family"], expected_family)
                self.assertIn(f"IP family {expected_family}; TLS verification enabled", result.stderr)
                self.assertIn('"finish"', (work / "pings.jsonl").read_text())
                if gateway_failures:
                    report = json.loads((work / ".scorebench/openrouter/result.json").read_text())
                    self.assertEqual(report["accounting_quality"], "exact")
                    self.assertEqual(report["accounting_basis"], "experiment")
                    self.assertEqual(report["excluded_infrastructure_requests"], 3)
                    self.assertEqual(report["excluded_provider_cost_unknown_requests"], 3)
                    self.assertIn("openrouter_experiment_usage", last)
                if receipt_lookup:
                    self.assertEqual(set(server.generation_reads), {"gen-2"})
                    self.assertEqual(count, 3 if delayed_receipt else 2)
                    self.assertEqual(next(r for r in ledger if r.get("reconciliation"))["reconciliation"]["source"], "openrouter_generation_api")
                    if not delayed_receipt:
                        self.assertEqual(server.generation_reads, ["gen-2"])
                        self.assertFalse((work / ".scorebench/openrouter/reentries.json").exists())
                if length_stop or early_stop:
                    self.assertEqual(count, 4 if compaction else 3)
                    recovery = json.loads((work / ".scorebench/openrouter/reentries.json").read_text())
                    self.assertEqual(len(recovery["attempts"]), 1)
                    native = [json.loads(line) for line in (work / ".scorebench/openrouter/native-output.jsonl").read_text().splitlines()]
                    self.assertEqual({e["sessionID"] for e in native}, {recovery["session_id"]})
                    self.assertTrue(any(e.get("part", {}).get("reason") == ("stop" if early_stop else "length") for e in native))
                    self.assertEqual(recovery["attempts"][0]["reason"], "receipt_recovery" if delayed_receipt else "early_stop" if early_stop else "length")
                    self.assertIn('"resume"', (work / "pings.jsonl").read_text())
                    if compaction:
                        export_env = dict(env, XDG_DATA_HOME=str(private_sessions))
                        with tempfile.TemporaryFile() as out:
                            exported = subprocess.run([binary, "--pure", "export", recovery["session_id"]],
                                env=export_env, cwd=work, stdout=out, stderr=subprocess.PIPE, timeout=30)
                            self.assertEqual(exported.returncode, 0)
                            out.seek(0)
                            messages = json.load(out)["messages"]
                        self.assertTrue(any(m["info"].get("summary") for m in messages))
                        self.assertTrue(any(p.get("type") == "compaction" for m in messages for p in m.get("parts", [])))
                print(f"{kind}/{model}/{effort}: {count} real CLI requests, tool round trip, zero baseline and final snapshot verified")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"])

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_gateway_retry(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"], gateway_failures=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_PI_BIN"), "set SCOREBENCH_TEST_PI_BIN")
    def test_pi_gateway_retry(self):
        self.run_agent("Pi", os.environ["SCOREBENCH_TEST_PI_BIN"], gateway_failures=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_length_stop_resumes_same_session_with_cumulative_cost(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"], effort="high", length_stop=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_retained_recovery_checks_then_resumes_without_reset(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"], effort="high", length_stop=True, retained_recovery=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_delayed_receipt_repair_then_same_session_recovery(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"], early_stop=True,
                       retained_recovery=True, receipt_lookup=True, delayed_receipt=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_empty_stop_automatically_continues_same_session(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"], early_stop=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_empty_stop_retained_recovery(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"], early_stop=True, retained_recovery=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_compaction_then_empty_stop_continues(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"], early_stop=True, compaction=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_PI_BIN"), "set SCOREBENCH_TEST_PI_BIN")
    def test_pi(self):
        self.run_agent("Pi", os.environ["SCOREBENCH_TEST_PI_BIN"])

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_PI_BIN"), "set SCOREBENCH_TEST_PI_BIN")
    def test_pi_ipv4_with_receipt_lookup(self):
        self.run_agent("Pi", os.environ["SCOREBENCH_TEST_PI_BIN"], receipt_lookup=True, ip_family="4")

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_PI_BIN"), "set SCOREBENCH_TEST_PI_BIN")
    def test_pi_explicit_auto_override_with_receipt_lookup(self):
        self.run_agent("Pi", os.environ["SCOREBENCH_TEST_PI_BIN"], receipt_lookup=True, ip_family="auto")

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_ipv4_retained_recovery(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"],
                       early_stop=True, retained_recovery=True, ip_family="4")

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_OPENCODE_BIN"), "set SCOREBENCH_TEST_OPENCODE_BIN")
    def test_opencode_missing_receipt_uses_generation_lookup(self):
        self.run_agent("OpenCode", os.environ["SCOREBENCH_TEST_OPENCODE_BIN"], receipt_lookup=True)

    @unittest.skipUnless(os.environ.get("SCOREBENCH_TEST_PI_BIN"), "set SCOREBENCH_TEST_PI_BIN")
    def test_pi_missing_receipt_uses_generation_lookup(self):
        self.run_agent("Pi", os.environ["SCOREBENCH_TEST_PI_BIN"], receipt_lookup=True)

    def test_offered_models_and_efforts_reach_provider_unchanged(self):
        binaries = [(kind, os.environ.get(name)) for kind, name in (
            ("Pi", "SCOREBENCH_TEST_PI_BIN"), ("OpenCode", "SCOREBENCH_TEST_OPENCODE_BIN"))]
        if not any(binary for _, binary in binaries):
            self.skipTest("set native CLI paths")
        for kind, binary in binaries:
            if not binary:
                continue
            for model in ("openai/gpt-6-astra", "anthropic/claude-fable-5.1",
                          "x-ai/grok-4.6", "deepseek/deepseek-v4.1-flash"):
                for effort in ("low", "medium", "high"):
                    with self.subTest(kind=kind, model=model, effort=effort):
                        self.run_agent(kind, binary, model, effort)


if __name__ == "__main__":
    unittest.main()
