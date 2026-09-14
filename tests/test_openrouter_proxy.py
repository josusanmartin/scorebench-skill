import json
import io
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "scorebench" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import openrouter_proxy as orp  # noqa: E402
import token_usage as usage_helper  # noqa: E402

TOKEN_USAGE = SCRIPTS / "token_usage.py"

NONSTREAM_BODY = json.dumps({
    "id": "gen-nonstream",
    "model": "anthropic/claude",
    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
    "usage": {
        "prompt_tokens": 300, "completion_tokens": 50, "total_tokens": 350, "cost": 0.01,
        "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    },
}).encode()

# usage rides the final SSE chunk (empty choices), exactly like OpenRouter
STREAM_CHUNKS = [
    b'data: {"id":"gen-stream","choices":[{"delta":{"content":"h"}}]}\n\n',
    b'data: {"id":"gen-stream","choices":[{"delta":{"content":"i"}}]}\n\n',
    b'data: {"id":"gen-stream","model":"anthropic/claude","choices":[],'
    b'"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":0.004,'
    b'"prompt_tokens_details":{"cached_tokens":0,"cache_write_tokens":0}}}\n\n',
    b'data: [DONE]\n\n',
]

ANTHROPIC_STREAM_CHUNKS = [
    b'event: message_start\n',
    b'data: {"type":"message_start","message":{"id":"msg-anthropic","model":"anthropic/claude",'
    b'"usage":{"input_tokens":100,"output_tokens":0,"cache_read_input_tokens":70,'
    b'"cache_creation_input_tokens":20}}}\n\n',
    b'event: message_delta\n',
    b'data: {"type":"message_delta","usage":{"output_tokens":8,"cost":0.01}}\n\n',
    b'data: {"type":"message_stop"}\n\n',
]

RESPONSES_STREAM_CHUNKS = [
    b'event: response.created\n',
    b'data: {"type":"response.created","response":{"id":"resp-codex",'
    b'"model":"openai/gpt-codex","usage":null}}\n\n',
    b'event: response.completed\n',
    b'data: {"type":"response.completed","response":{"id":"resp-codex",'
    b'"model":"openai/gpt-codex","usage":{"input_tokens":140,"output_tokens":12,'
    b'"total_tokens":152,"cost":0.003,"input_tokens_details":{"cached_tokens":90},'
    b'"output_tokens_details":{"reasoning_tokens":4}}}}\n\n',
]


class FakeUpstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        # Echo the auth header the proxy injected so the test can assert it.
        auth = self.headers.get("Authorization", "")
        if b'"error": true' in body or b'"error":true' in body:
            error_body = json.dumps({
                "id": "gen-error",
                "error": {"message": "provider stopped"},
                "usage": {
                    "prompt_tokens": 25,
                    "completion_tokens": 0,
                    "total_tokens": 25,
                    "cost": 0.00001,
                },
            }).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return
        streaming = b'"stream": true' in body or b'"stream":true' in body
        if streaming:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("X-Seen-Auth", auth)
            self.end_headers()
            if self.path.endswith("/v1/messages"):
                chunks = ANTHROPIC_STREAM_CHUNKS
            elif self.path.endswith("/responses"):
                chunks = RESPONSES_STREAM_CHUNKS
            else:
                chunks = STREAM_CHUNKS
            for chunk in chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
                if b'"slow": true' in body and chunk == chunks[0]:
                    self.server.release.wait(3)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Seen-Auth", auth)
            self.send_header("Content-Length", str(len(NONSTREAM_BODY)))
            self.end_headers()
            self.wfile.write(NONSTREAM_BODY)


class OpenRouterProxyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.log = Path(self.tempdir.name) / "or.jsonl"
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        up_host, up_port = self.upstream.server_address
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), orp.Handler)
        self.proxy.upstream = f"http://{up_host}:{up_port}"
        self.proxy.api_key = "test-key-123"
        self.proxy.usage_log = orp.UsageLog(self.log)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        px_host, px_port = self.proxy.server_address
        self.base = f"http://{px_host}:{px_port}/api/v1"

    def tearDown(self):
        self.proxy.shutdown(); self.proxy.server_close()
        self.upstream.shutdown(); self.upstream.server_close()
        self.tempdir.cleanup()

    def _post(self, payload):
        req = urllib.request.Request(
            self.base + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer client-placeholder"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()

    def _log_lines(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    def test_nonstreaming_passthrough_and_usage_capture(self):
        status, headers, body = self._post({"model": "anthropic/claude", "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(body, NONSTREAM_BODY)  # returned byte-for-byte
        self.assertEqual(headers.get("X-Seen-Auth"), "Bearer test-key-123")  # proxy injected the real key
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["usage"]["cost"], 0.01)
        self.assertEqual(lines[0]["id"], "gen-nonstream")

    def test_stream_first_delta_arrives_before_generation_finishes(self):
        self.upstream.release = threading.Event()
        request = urllib.request.Request(self.base + "/chat/completions",
            data=json.dumps({"stream": True, "slow": True}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                self.assertEqual(response.readline(), STREAM_CHUNKS[0].splitlines(keepends=True)[0])
                self.assertEqual(self._log_lines(), [])
                self.upstream.release.set()
                response.read()
            self.assertEqual(self._log_lines()[0]["usage"]["cost"], 0.004)
        finally:
            self.upstream.release.set()

    def test_wrong_native_model_is_rejected_without_billable_request(self):
        self.proxy.allowed_models = {"assigned/model"}
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self._post({"model": "wrong/model", "messages": []})
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()
        self.assertEqual(self._log_lines(), [])

    def test_disconnect_while_sending_headers_still_drains_billed_usage(self):
        handler = object.__new__(orp.Handler)
        handler.server, handler.command, handler.path = self.proxy, "POST", "/chat/completions"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock(side_effect=BrokenPipeError)
        handler.wfile = mock.Mock()
        upstream = io.BytesIO(b"".join(STREAM_CHUNKS))
        upstream.status, upstream.headers = 200, {"Content-Type": "text/event-stream"}
        handler._relay(upstream, record_usage=True)
        self.assertEqual(self._log_lines()[0]["usage"]["cost"], 0.004)
        handler.wfile.write.assert_not_called()

    def test_ambiguous_generation_timeout_is_not_zero_cost(self):
        handler = object.__new__(orp.Handler)
        handler.server, handler.command, handler.path = self.proxy, "POST", "/chat/completions"
        handler.headers, handler.rfile = {}, io.BytesIO()
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        handler.wfile = io.BytesIO()
        with mock.patch.object(orp, "open_upstream", side_effect=TimeoutError) as opened:
            handler._forward()
        self.assertEqual(opened.call_count, 1)
        handler.send_response.assert_called_once_with(502)
        self.assertIn("cost are unknown", self._log_lines()[0]["accounting_error"])
        with self.assertRaises(SystemExit):
            usage_helper.openrouter_jsonl_snapshot(self.log)

    def test_connect_failures_retry_same_request_without_losing_accounting(self):
        real_open = orp.open_upstream
        attempts = []
        def open_request(request):
            attempts.append(request)
            if len(attempts) < 3:
                raise urllib.error.URLError(orp.UnsentRequestError(socket.gaierror(-3, "secret-host")))
            return real_open(request)
        with mock.patch.object(orp, "open_upstream", side_effect=open_request), mock.patch.object(orp.time, "sleep") as sleep:
            status, _, _ = self._post({"model": "anthropic/claude", "messages": []})
        self.assertEqual(status, 200)
        self.assertEqual(len(attempts), 3)
        self.assertIs(attempts[0], attempts[2])
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(usage_helper.openrouter_jsonl_snapshot(self.log).cost_usd, 0.01)
        diagnostics = self.log.with_name("transport-errors.jsonl")
        self.assertEqual(diagnostics.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("secret-host", diagnostics.read_text())
        self.assertNotIn("test-key-123", diagnostics.read_text())
        self.assertEqual(len(diagnostics.read_text().splitlines()), 2)

    def test_real_refused_connection_recovers_when_listener_becomes_available(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream, bind_and_activate=False)
        upstream.server_bind()
        self.proxy.upstream = f"http://127.0.0.1:{upstream.server_address[1]}"
        original = orp._HTTPConnection.connect
        started = threading.Event()
        def connect(connection):
            try:
                return original(connection)
            except orp.UnsentRequestError:
                upstream.server_activate()
                threading.Thread(target=upstream.serve_forever, daemon=True).start()
                started.set()
                raise
        try:
            with mock.patch.object(orp._HTTPConnection, "connect", connect), mock.patch.object(orp.time, "sleep"):
                self.assertEqual(self._post({"messages": []})[0], 200)
            self.assertTrue(started.is_set())
            self.assertEqual(usage_helper.openrouter_jsonl_snapshot(self.log).cost_usd, 0.01)
            diagnostics = json.loads(self.log.with_name("transport-errors.jsonl").read_text())
            self.assertEqual(diagnostics["exception_type"], "ConnectionRefusedError")
            self.assertEqual(diagnostics["phase"], "connect")
        finally:
            if started.is_set():
                upstream.shutdown()
            upstream.server_close()

    def test_exhausted_unsent_retries_do_not_poison_future_usage(self):
        error = urllib.error.URLError(orp.UnsentRequestError(ConnectionRefusedError()))
        with mock.patch.object(orp, "open_upstream", side_effect=error) as opened, mock.patch.object(orp.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._post({"messages": []})
            self.assertEqual(raised.exception.code, 502)
            raised.exception.close()
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(self._log_lines(), [])
        self.assertFalse(self.proxy.usage_log.blocked.is_set())
        self._post({"messages": []})
        self.assertEqual(usage_helper.openrouter_jsonl_snapshot(self.log).cost_usd, 0.01)

    def test_bad_tls_certificate_is_not_retried_or_treated_as_billed(self):
        error = urllib.error.URLError(orp.UnsentRequestError(ssl.SSLCertVerificationError("secret")))
        with mock.patch.object(orp, "open_upstream", side_effect=error) as opened:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._post({"messages": []})
            self.assertNotIn(b"secret", raised.exception.read())
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self._log_lines(), [])

    def test_transport_phase_comes_from_connect_not_exception_name(self):
        for connection in (orp._HTTPConnection, orp._HTTPSConnection):
            conn = connection("localhost", timeout=1)
            with self.subTest(connection=connection), mock.patch.object(conn, "_create_connection", side_effect=TimeoutError()):
                with self.assertRaises(orp.UnsentRequestError):
                    conn.connect()
        # An identical exception outside connect must remain an accounting gap.
        with mock.patch.object(orp, "open_upstream", side_effect=TimeoutError()) as opened:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._post({"messages": []})
            raised.exception.close()
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(self._log_lines()[0]["phase"], "request_or_headers")

    def test_unknown_acceptance_blocks_further_inference_without_replaying(self):
        with mock.patch.object(orp, "open_upstream", side_effect=orp.HTTPException("secret-request")) as opened:
            for status in (502, 409):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self._post({"messages": []})
                self.assertEqual(raised.exception.code, status)
                self.assertNotIn(b"secret-request", raised.exception.read())
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(self._log_lines()), 1)
        with self.assertRaises(SystemExit):
            usage_helper.openrouter_jsonl_snapshot(self.log)

    def test_trailing_stream_error_keeps_only_conclusive_receipts(self):
        cases = [
            (STREAM_CHUNKS[:3], True),
            (RESPONSES_STREAM_CHUNKS, True),
            (ANTHROPIC_STREAM_CHUNKS, True),
            (ANTHROPIC_STREAM_CHUNKS[:-1], False),
            (STREAM_CHUNKS[:1], False),
        ]
        for chunks, complete in cases:
            with self.subTest(chunks=chunks):
                self.log.write_text("")
                self.proxy.usage_log.blocked.clear()
                handler = object.__new__(orp.Handler)
                handler.server, handler.command, handler.path = self.proxy, "POST", "/chat/completions"
                handler.wfile = io.BytesIO()
                upstream = mock.Mock()
                upstream.read1.side_effect = [b"".join(chunks), ConnectionResetError()]
                handler._pump_stream(upstream, record_usage=True)
                self.assertEqual(len(self._log_lines()), 1)
                if complete:
                    self.assertGreater(usage_helper.openrouter_jsonl_snapshot(self.log).cost_usd, 0)
                    self.assertFalse(self.proxy.usage_log.blocked.is_set())
                else:
                    self.assertTrue(self.proxy.usage_log.blocked.is_set())
                    with self.assertRaises(SystemExit):
                        usage_helper.openrouter_jsonl_snapshot(self.log)

    def test_clean_eof_after_partial_usage_still_refuses_accounting(self):
        handler = object.__new__(orp.Handler)
        handler.server, handler.command, handler.path = self.proxy, "POST", "/messages"
        handler.wfile = io.BytesIO()
        handler._pump_stream(io.BytesIO(b"".join(ANTHROPIC_STREAM_CHUNKS[:-1])), record_usage=True)
        self.assertTrue(self.proxy.usage_log.blocked.is_set())
        with self.assertRaises(SystemExit):
            usage_helper.openrouter_jsonl_snapshot(self.log)

    def test_invalid_cost_never_certifies_a_final_stream_receipt(self):
        for cost in (None, True, -1, float("inf"), float("nan"), 10**500):
            with self.subTest(cost=cost):
                self.assertFalse(orp.Handler._complete_usage({
                    "prompt_tokens": 10, "completion_tokens": 2, "cost": cost}))

    def test_later_inflight_receipts_do_not_erase_an_unknown_request(self):
        self._post({"messages": []})
        self.proxy.usage_log.error("unknown accepted generation")
        handler = object.__new__(orp.Handler)
        handler.server, handler.command, handler.path = self.proxy, "POST", "/chat/completions"
        handler.wfile = io.BytesIO()
        handler._pump_stream(io.BytesIO(b"".join(STREAM_CHUNKS)), record_usage=True)
        lines = self._log_lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[-1]["usage"]["cost"], 0.004)
        self.assertTrue(self.proxy.usage_log.blocked.is_set())
        with self.assertRaises(SystemExit):
            usage_helper.openrouter_jsonl_snapshot(self.log)

    def test_upstream_504_without_usage_cannot_be_treated_as_free(self):
        handler = object.__new__(orp.Handler)
        handler.server, handler.command, handler.path = self.proxy, "POST", "/chat/completions"
        handler.send_response, handler.send_header, handler.end_headers = mock.Mock(), mock.Mock(), mock.Mock()
        handler.wfile = io.BytesIO()
        upstream = io.BytesIO(b'{"error":{"message":"gateway timeout"}}')
        upstream.status, upstream.headers = 504, {"Content-Type": "application/json"}
        handler._relay(upstream, record_usage=True)
        self.assertEqual(self._log_lines()[0]["accounting_error"], "generation response omitted usage")
        with self.assertRaises(SystemExit):
            usage_helper.openrouter_jsonl_snapshot(self.log)

    def test_nonstreaming_responses_envelope_usage_capture(self):
        body = json.dumps({
            "type": "response.completed",
            "response": {
                "id": "resp-nonstream",
                "model": "openai/gpt-codex",
                "usage": {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23, "cost": 0.001},
            },
        }).encode()
        self.proxy.usage_log.path.write_text("")
        # Exercise the parser directly because the fake upstream's normal
        # nonstream response is a chat-completions body.
        handler = object.__new__(orp.Handler)
        handler.server = self.proxy
        handler._record_body_usage(body)
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["id"], "resp-nonstream")
        self.assertEqual(lines[0]["usage"]["cost"], 0.001)

    def test_streaming_passthrough_and_final_chunk_usage_capture(self):
        status, headers, body = self._post({"model": "anthropic/claude", "messages": [], "stream": True})
        self.assertEqual(status, 200)
        self.assertEqual(body, b"".join(STREAM_CHUNKS))  # SSE relayed unchanged
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)  # only the final chunk carried usage
        self.assertEqual(lines[0]["usage"]["cost"], 0.004)

    def test_anthropic_stream_fragments_are_merged_into_one_usage_record(self):
        request = urllib.request.Request(
            self.base.removesuffix("/api/v1") + "/v1/messages",
            data=json.dumps({"model": "anthropic/claude", "messages": [], "stream": True}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer placeholder"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.read(), b"".join(ANTHROPIC_STREAM_CHUNKS))
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["id"], "msg-anthropic")
        self.assertEqual(lines[0]["usage"]["input_tokens"], 100)
        self.assertEqual(lines[0]["usage"]["output_tokens"], 8)
        self.assertEqual(lines[0]["usage"]["cache_read_input_tokens"], 70)
        self.assertEqual(lines[0]["usage"]["cost"], 0.01)
        snapshot = usage_helper.openrouter_usage_snapshot(lines[0]["usage"])
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.total_tokens, 128)
        self.assertEqual(snapshot.input_tokens, 100)
        self.assertEqual(snapshot.cache_creation_tokens, 20)
        self.assertEqual(snapshot.cache_read_tokens, 70)

    def test_responses_stream_nested_usage_is_captured(self):
        request = urllib.request.Request(
            self.base + "/responses",
            data=json.dumps({"model": "openai/gpt-codex", "input": [], "stream": True}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer placeholder"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.read(), b"".join(RESPONSES_STREAM_CHUNKS))
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["id"], "resp-codex")
        self.assertEqual(lines[0]["model"], "openai/gpt-codex")
        self.assertEqual(lines[0]["usage"]["total_tokens"], 152)
        self.assertEqual(lines[0]["usage"]["cost"], 0.003)

    def test_token_usage_reads_the_proxy_log(self):
        # baseline on the empty log, then two calls, then flags -> exact tokens + cost
        state = Path(self.tempdir.name) / "state.json"
        subprocess.run([sys.executable, str(TOKEN_USAGE), "start", "--state", str(state),
                        "--openrouter-jsonl", str(self.log)], check=True, capture_output=True)
        self._post({"model": "anthropic/claude", "messages": []})
        self._post({"model": "anthropic/claude", "messages": [], "stream": True})
        out = subprocess.run([sys.executable, str(TOKEN_USAGE), "flags", "--state", str(state),
                              "--openrouter-jsonl", str(self.log)], check=True, capture_output=True, text=True).stdout
        self.assertIn("--cost-usd 0.014", out)          # 0.01 + 0.004
        self.assertIn("--total-tokens 470", out)         # (300+50) + (100+20)
        self.assertIn("--usage-source openrouter", out)
        self.assertIn("--tokens-total-source openrouter_usage", out)

    def test_billable_error_usage_is_captured(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post({"model": "anthropic/claude", "messages": [], "error": True})
        self.assertEqual(caught.exception.code, 429)
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["id"], "gen-error")
        self.assertEqual(lines[0]["usage"]["cost"], 0.00001)


if __name__ == "__main__":
    unittest.main()
