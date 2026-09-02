import json
import subprocess
import sys
import tempfile
import threading
import unittest
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
            chunks = ANTHROPIC_STREAM_CHUNKS if self.path.endswith("/v1/messages") else STREAM_CHUNKS
            for chunk in chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
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
