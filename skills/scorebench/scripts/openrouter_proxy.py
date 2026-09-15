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
from http.client import HTTPException
import json
import math
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from openrouter_generations import generation_usage, lookup_generation, stream_gap
from openrouter_transport import (
    UnsentRequestError, _HTTPConnection, _HTTPSConnection,
    configured_ip_family, openrouter_opener,
)

DEFAULT_UPSTREAM = "https://openrouter.ai"
# Hop-by-hop and content-coding headers we must not blindly forward.
_SKIP_REQUEST_HEADERS = {"host", "authorization", "x-api-key", "proxy-authorization", "content-length", "accept-encoding", "connection"}
_SKIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "content-encoding", "keep-alive"}


def open_upstream(request):
    return openrouter_opener().open(request, timeout=600)


class UsageLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._idle = threading.Condition()
        self._active = 0
        self._reconciling = 0
        self.blocked = threading.Event()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file if absent so `token_usage.py start` sees a zero
        # baseline; never truncate, so a restarted proxy keeps prior usage and
        # the run baseline/delta stays correct.
        path.touch(exist_ok=True)

    def request_started(self) -> None:
        with self._idle:
            self._active += 1

    def request_finished(self) -> None:
        with self._idle:
            self._active -= 1
            self._idle.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: self._active == 0, timeout)

    def begin_reconciliation(self) -> None:
        with self._idle:
            self._reconciling += 1

    def end_reconciliation(self) -> None:
        with self._idle:
            self._reconciling -= 1
            self._idle.notify_all()

    def wait_reconciled(self, timeout: float) -> bool:
        with self._idle:
            return self._idle.wait_for(lambda: self._reconciling == 0, timeout)

    def record(self, response_obj: dict) -> bool:
        usage = response_obj.get("usage")
        if not isinstance(usage, dict) or not usage:
            return False
        record = {
            "id": response_obj.get("id"),
            "model": response_obj.get("model"),
            "usage": usage,
        }
        if response_obj.get("reconciliation"):
            record["reconciliation"] = response_obj["reconciliation"]
        self._append(record)
        return True

    def error(self, reason: str, **details) -> None:
        self.blocked.set()
        self._append({"accounting_error": reason, **details})

    def transport_error(self, exc, *, request_id, phase, attempt):
        cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(cause, UnsentRequestError):
            cause = cause.cause
        details = {"request_id": request_id, "phase": phase, "attempt": attempt,
                   "ip_family": configured_ip_family(),
                   "exception_type": type(cause).__name__, "errno": getattr(cause, "errno", None)}
        # Do not log exception strings, URLs, headers, prompts, or credentials.
        self._append({"timestamp": time.time(), **details}, self.path.with_name("transport-errors.jsonl"))
        return details

    def _append(self, record: dict, path: Path | None = None) -> None:
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._lock:
            with os.fdopen(os.open(path or self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a", encoding="utf-8") as handle:
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
        self.server.usage_log.request_started()
        try:
            self._forward()
        finally:
            self.server.usage_log.request_finished()

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        if self._generation_request() and not self.server.usage_log.wait_reconciled(25):
            self._send_proxy_error(503, "OpenRouter receipt lookup pending; no inference request sent")
            return
        if self._generation_request() and self.server.usage_log.blocked.is_set():
            self._send_proxy_error(409, "OpenRouter accounting is incomplete; retain this run for review")
            return
        allowed_models = getattr(self.server, "allowed_models", None)
        self.request_model = None
        if self._generation_request():
            try:
                selected = json.loads(body or b"{}").get("model")
            except (ValueError, AttributeError):
                selected = None
            self.request_model = selected if isinstance(selected, str) else None
            if allowed_models and selected not in allowed_models:
                self.send_error(400, "model does not match the assigned OpenRouter recipe")
                self.close_connection = True
                return
        url = self.server.upstream.rstrip("/") + self.path  # type: ignore[attr-defined]
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _SKIP_REQUEST_HEADERS
        }
        headers["Authorization"] = f"Bearer {self.server.api_key}"  # type: ignore[attr-defined]
        headers["Accept-Encoding"] = "identity"  # keep the body parseable + forwardable
        request = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        request_id = uuid4().hex
        for attempt in range(1, 4):
            if self._generation_request() and not self.server.usage_log.wait_reconciled(25):
                self._send_proxy_error(503, "OpenRouter receipt lookup pending; no inference request sent")
                return
            if self._generation_request() and self.server.usage_log.blocked.is_set():
                self._send_proxy_error(409, "OpenRouter accounting is incomplete; retain this run for review")
                return
            try:
                upstream = open_upstream(request)
                break
            except urllib.error.HTTPError as exc:
                # Some provider failures can still report billable usage.
                with exc:
                    self._relay(exc, record_usage=True)
                return
            except (urllib.error.URLError, OSError, HTTPException) as exc:
                cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
                unsent = isinstance(cause, UnsentRequestError)
                details = self.server.usage_log.transport_error(exc, request_id=request_id,
                    phase="connect" if unsent else "request_or_headers", attempt=attempt)
                if unsent and attempt < 3 and not isinstance(cause.cause, ssl.SSLCertVerificationError):
                    time.sleep(attempt)
                    continue
                if self._generation_request() and not unsent:
                    self.server.usage_log.error(
                        "upstream transport failure; generation acceptance and cost are unknown", **details)
                self._send_proxy_error(502, "upstream connection failed before sending request" if unsent
                    else "upstream transport failed; generation acceptance and cost are unknown", request_id)
                return
        with upstream:
            self._relay(upstream, record_usage=True)

    def _send_proxy_error(self, status, message, request_id=None):
        body = json.dumps({"error": {"code": status, "message": message, "request_id": request_id}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _relay(self, upstream, *, record_usage: bool) -> None:
        status = getattr(upstream, "status", None) or upstream.getcode() or 200
        content_type = upstream.headers.get("Content-Type", "")
        streaming = "text/event-stream" in content_type.lower()
        try:
            body = None if streaming else upstream.read()
        except (OSError, HTTPException):
            if record_usage and self._generation_request():
                self.server.usage_log.error("generation response interrupted before usage")
            raise
        if body is not None and record_usage:
            recorded = self._record_body_usage(body)
            if not recorded and (200 <= status < 400 or status >= 500) and self._generation_request():
                self.server.usage_log.error("generation response omitted usage")
        client_connected = True
        try:
            self.send_response(status)
            for key, value in upstream.headers.items():
                if key.lower() in _SKIP_RESPONSE_HEADERS:
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            client_connected = False
        try:
            if streaming:
                self._pump_stream(upstream, record_usage=record_usage, client_connected=client_connected)
            elif client_connected:
                self.wfile.write(body or b"")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.close_connection = True

    def _generation_request(self) -> bool:
        return self.command == "POST" and self.path.split("?", 1)[0].endswith(
            ("/chat/completions", "/messages", "/responses", "/completions")
        )

    def _record_body_usage(self, body: bytes) -> bool:
        try:
            obj = json.loads(body)
        except (ValueError, TypeError):
            return False
        if isinstance(obj, dict):
            # Persist usage before exposing the response status/body. A client
            # disconnect after OpenRouter bills must not make the run cheaper.
            response = obj.get("response")
            if isinstance(obj.get("usage"), dict):
                return self.server.usage_log.record(obj)  # type: ignore[attr-defined]
            elif isinstance(response, dict):
                return self.server.usage_log.record(response)  # type: ignore[attr-defined]
        return False

    def _pump_stream(self, upstream, *, record_usage: bool, client_connected: bool = True) -> None:
        buffer = b""
        response_obj: dict = {"usage": {}}
        settlement_held = False
        try:
            while True:
                try:
                    # read1 forwards available bytes without waiting to fill
                    # the buffer, including small token deltas.
                    chunk = getattr(upstream, "read1", upstream.read)(65536)
                except (OSError, HTTPException, EOFError) as exc:
                    if record_usage:
                        details = self.server.usage_log.transport_error(exc, request_id=uuid4().hex,
                            phase="stream", attempt=1)
                        if response_obj.get("complete"):
                            self.server.usage_log.record(response_obj)
                        else:
                            self._reconcile_stream(response_obj, "generation stream interrupted before final usage", **details)
                    return
                if not chunk:
                    break
                if record_usage:
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        self._scan_sse_line(line, response_obj)
                    # A client can start its next turn as soon as it sees a
                    # terminal event, before this connection reaches EOF.
                    if response_obj.get("terminal_seen") and not settlement_held:
                        self.server.usage_log.begin_reconciliation()
                        settlement_held = True
                if client_connected:
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        client_connected = False
            if record_usage and buffer:
                self._scan_sse_line(buffer, response_obj)
            if record_usage and response_obj.get("complete"):
                self.server.usage_log.record(response_obj)
            elif record_usage and self._generation_request():
                self._reconcile_stream(response_obj, "generation stream omitted final usage")
        finally:
            if settlement_held:
                self.server.usage_log.end_reconciliation()

    def _reconcile_stream(self, response_obj: dict, reason: str, **details) -> None:
        log = self.server.usage_log
        generation_id = response_obj.get("id")
        model = getattr(self, "request_model", None) or response_obj.get("model")
        gap = {"accounting_error": reason, "generation_id": generation_id}
        log.begin_reconciliation()
        try:
            if not stream_gap(gap) or not model or response_obj.get("identity_conflict"):
                raise ValueError("stream has no consistent generation and model binding")
            lookup = lookup_generation(generation_id, model, upstream=self.server.upstream,
                                       api_key=self.server.api_key, require_final=True)
            data = lookup["data"]
            usage = generation_usage(data, generation_id, model, model_alias=lookup.get("model_alias"))
            if response_obj.get("model") not in (None, model, data["model"]):
                raise ValueError("generation lookup model differs from the observed stream")
            if not data.get("finish_reason"):
                raise ValueError("generation termination is not confirmed")
            observed = response_obj.get("usage", {})
            for field, recovered in (("prompt_tokens", usage["prompt_tokens"]),
                                     ("completion_tokens", usage["completion_tokens"]), ("cost", usage["cost"])):
                if field in observed and (type(observed[field]) not in (int, float)
                        or not math.isfinite(observed[field]) or observed[field] < 0 or observed[field] > recovered):
                    raise ValueError("generation lookup contradicts observed stream usage")
            # Persist the missing-receipt reason and provider evidence with the
            # replacement receipt, without replaying the billable request.
            log.record({"id": generation_id, "model": model, "usage": usage,
                        "reconciliation": {**lookup, "reason": reason, "observed_usage": observed}})
        except (ValueError, OverflowError, OSError, HTTPException) as exc:
            log.error(reason, generation_id=generation_id, model=response_obj.get("model") or model,
                      lookup_error_type=type(exc).__name__, **details)
        finally:
            log.end_reconciliation()

    def _scan_sse_line(self, line: bytes, response_obj: dict) -> None:
        line = line.strip()
        if not line.startswith(b"data:"):
            return
        payload = line[len(b"data:"):].strip()
        if payload == b"[DONE]":
            response_obj["terminal_seen"] = True
            response_obj["complete"] = self._complete_usage(response_obj["usage"])
            return
        if not payload:
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
        for field in ("id", "model"):
            value = obj.get(field) or message.get(field) or response.get(field)
            if value:
                if response_obj.get(field) not in (None, value):
                    response_obj["identity_conflict"] = True
                response_obj[field] = value
        terminal = (obj.get("type") in ("response.completed", "response.incomplete", "message_stop")
                    or (isinstance(obj.get("usage"), dict) and obj.get("choices") == [])
                    or (isinstance(obj.get("choices"), list) and any(
                        isinstance(choice, dict) and choice.get("finish_reason") is not None
                        for choice in obj["choices"])))
        if terminal:
            response_obj["terminal_seen"] = True
            response_obj["complete"] = self._complete_usage(response_obj["usage"])

    @staticmethod
    def _complete_usage(usage):
        keys = ("prompt_tokens", "completion_tokens") if "prompt_tokens" in usage else ("input_tokens", "output_tokens")
        try:
            return all(type(usage.get(key)) in (int, float) and math.isfinite(usage[key]) and usage[key] >= 0
                       for key in (*keys, "cost"))
        except OverflowError:
            return False

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
    try:
        ip_family = configured_ip_family()
    except ValueError as exc:
        parser.error(str(exc))

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
    print(f"ScoreBench OpenRouter transport: IP family {ip_family}; TLS verification enabled", file=sys.stderr)
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
