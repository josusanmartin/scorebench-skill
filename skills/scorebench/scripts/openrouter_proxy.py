#!/usr/bin/env python3
"""Transparent OpenRouter proxy that logs exact per-response token usage and cost.

OpenRouter never exposes a per-key TOKEN total — only cost is aggregated per
key, while exact tokens live only in each response's `usage` object. To measure
a run's exact tokens and cost in real time without depending on any particular
coding harness, point the harness's OpenAI base URL at this proxy:

    OPENAI_BASE_URL=http://127.0.0.1:<port>/api/v1   (or the harness's equivalent)

The proxy forwards every request to OpenRouter with the single OPENROUTER_API_KEY,
returns each response byte-for-byte unchanged, and appends that response's `usage`
(tokens + USD cost) as one JSON line to the usage log. `token_usage.py
--openrouter-jsonl <log>` reads that log, so `scorebench submit`/`run usage`
carry exact tokens and an authoritative dollar cost, for any harness.

Streaming is handled: the `usage` object arrives in the final SSE chunk, so the
proxy tees the stream to the client while scanning it for that chunk — it never
buffers the whole response, so it adds no latency.

Dependency-free (stdlib only). Reads OPENROUTER_API_KEY from the environment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_UPSTREAM = "https://openrouter.ai"
# Hop-by-hop and content-coding headers we must not blindly forward.
_SKIP_REQUEST_HEADERS = {"host", "authorization", "content-length", "accept-encoding", "connection"}
_SKIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding", "keep-alive"}


class UsageLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file if absent so `token_usage.py start` sees a zero
        # baseline; never truncate, so a restarted proxy keeps prior usage and
        # the run baseline/delta stays correct.
        path.touch(exist_ok=True)

    def record(self, response_obj: dict) -> None:
        usage = response_obj.get("usage")
        if not isinstance(usage, dict) or not usage:
            return
        record = {
            "id": response_obj.get("id"),
            "model": response_obj.get("model"),
            "usage": usage,
        }
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()


class Handler(BaseHTTPRequestHandler):
    # set on the server instance
    upstream: str
    api_key: str
    usage_log: UsageLog
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # keep the proxy quiet
        return

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = self.server.upstream.rstrip("/") + self.path  # type: ignore[attr-defined]
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _SKIP_REQUEST_HEADERS
        }
        headers["Authorization"] = f"Bearer {self.server.api_key}"  # type: ignore[attr-defined]
        headers["Accept-Encoding"] = "identity"  # keep the body parseable + forwardable
        request = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            upstream = urllib.request.urlopen(request, timeout=600)  # nosec B310 (fixed upstream host)
        except urllib.error.HTTPError as exc:
            # Some provider failures can still report billable usage.
            self._relay(exc, record_usage=True)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            body = json.dumps({"error": f"openrouter proxy upstream failure: {exc}"}).encode()
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        self._relay(upstream, record_usage=True)

    def _relay(self, upstream, *, record_usage: bool) -> None:
        status = getattr(upstream, "status", None) or upstream.getcode() or 200
        content_type = upstream.headers.get("Content-Type", "")
        streaming = "text/event-stream" in content_type.lower()
        body = None if streaming else upstream.read()
        if body is not None and record_usage:
            self._record_body_usage(body)
        self.send_response(status)
        for key, value in upstream.headers.items():
            if key.lower() in _SKIP_RESPONSE_HEADERS:
                continue
            self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            if streaming:
                self._pump_stream(upstream, record_usage=record_usage)
            else:
                self.wfile.write(body or b"")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    def _record_body_usage(self, body: bytes) -> None:
        try:
            obj = json.loads(body)
        except (ValueError, TypeError):
            return
        if isinstance(obj, dict):
            # Persist usage before exposing the response status/body. A client
            # disconnect after OpenRouter bills must not make the run cheaper.
            response = obj.get("response")
            if isinstance(obj.get("usage"), dict):
                self.server.usage_log.record(obj)  # type: ignore[attr-defined]
            elif isinstance(response, dict):
                self.server.usage_log.record(response)  # type: ignore[attr-defined]

    def _pump_stream(self, upstream, *, record_usage: bool) -> None:
        buffer = b""
        client_connected = True
        response_obj: dict = {"usage": {}}
        while True:
            chunk = upstream.read(65536)
            if not chunk:
                break
            if record_usage:
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._scan_sse_line(line, response_obj)
            if client_connected:
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Keep draining the billed upstream response so the final
                    # SSE usage event is still captured.
                    client_connected = False
        if record_usage and buffer:
            self._scan_sse_line(buffer, response_obj)
        if record_usage and response_obj["usage"]:
            self.server.usage_log.record(response_obj)  # type: ignore[attr-defined]

    def _scan_sse_line(self, line: bytes, response_obj: dict) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[len(b"data:"):].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        response = obj.get("response") if isinstance(obj.get("response"), dict) else {}
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            usage = message.get("usage")
        if not isinstance(usage, dict):
            usage = response.get("usage")
        if isinstance(usage, dict):
            self._merge_usage(response_obj["usage"], usage)
        response_obj["id"] = (
            obj.get("id") or message.get("id") or response.get("id") or response_obj.get("id")
        )
        response_obj["model"] = (
            obj.get("model") or message.get("model") or response.get("model") or response_obj.get("model")
        )

    @staticmethod
    def _merge_usage(target: dict, update: dict) -> None:
        """Merge cumulative stream counters from OpenAI and Anthropic skins."""
        for key, value in update.items():
            if isinstance(value, dict):
                nested = target.setdefault(key, {})
                if isinstance(nested, dict):
                    Handler._merge_usage(nested, value)
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                previous = target.get(key)
                target[key] = max(previous, value) if isinstance(previous, (int, float)) else value
            elif value is not None:
                target[key] = value

    do_POST = _handle
    do_GET = _handle
    do_PUT = _handle
    do_DELETE = _handle


def main() -> int:
    parser = argparse.ArgumentParser(description="Transparent OpenRouter usage-logging proxy")
    parser.add_argument("--log", default=os.environ.get("SCOREBENCH_OPENROUTER_LOG", ""), help="usage log path (or $SCOREBENCH_OPENROUTER_LOG); read by token_usage.py --openrouter-jsonl")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SCOREBENCH_OPENROUTER_PORT", "0")), help="listen port; 0 auto-assigns and prints the chosen one")
    parser.add_argument("--host", default="127.0.0.1", help="listen host (default loopback only)")
    parser.add_argument("--upstream", default=os.environ.get("OPENROUTER_BASE", DEFAULT_UPSTREAM), help="upstream base URL")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    if not args.log:
        print("no usage log path: pass --log or set SCOREBENCH_OPENROUTER_LOG", file=sys.stderr)
        return 2

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.upstream = args.upstream  # type: ignore[attr-defined]
    server.api_key = api_key  # type: ignore[attr-defined]
    server.usage_log = UsageLog(Path(args.log).expanduser())  # type: ignore[attr-defined]
    port = server.server_address[1]
    base_url = f"http://{args.host}:{port}/api/v1"
    # One machine-readable line for scripts, then human guidance on stderr.
    print(json.dumps({"base_url": base_url, "port": port, "log": str(server.usage_log.path)}), flush=True)  # type: ignore[attr-defined]
    print(f"OpenRouter proxy on {base_url}\n  point your harness base URL here; usage -> {server.usage_log.path}", file=sys.stderr, flush=True)  # type: ignore[attr-defined]
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
