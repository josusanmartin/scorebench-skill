import json
import io
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
        with mock.patch.object(orp.urllib.request, "urlopen", side_effect=TimeoutError):
            handler._forward()
        handler.send_response.assert_called_once_with(502)
        self.assertIn("cost are unknown", self._log_lines()[0]["accounting_error"])
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
