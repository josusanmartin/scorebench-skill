import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/scorebench/scripts"
sys.path.insert(0, str(SCRIPTS))
from openrouter_gaps import ACK_FILE, load_ack, prepare_ack
from token_usage import openrouter_jsonl_snapshot
from openrouter_generations import generation_usage, lookup_generation, stream_gap


@pytest.fixture
def retained(tmp_path):
    log, state = tmp_path / "usage.jsonl", tmp_path / "token-state.json"
    log.touch()
    subprocess.run([sys.executable, str(SCRIPTS / "token_usage.py"), "start", "--state", str(state),
                    "--openrouter-jsonl", str(log)], check=True, capture_output=True)
    receipt = {"id": "gen-first", "usage": {"prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 10}, "cost": 0.1}}
    gap = {"accounting_error": "upstream transport failure; generation acceptance and cost are unknown",
           "phase": "request_or_headers", "request_id": "a" * 32, "exception_type": "SSLEOFError"}
    log.write_text(json.dumps(receipt) + "\n" + json.dumps(gap) + "\n")
    return log, state, receipt, gap


def ack_for(log, state):
    return prepare_ack(log, state, run_id="run-1", session_id="ses_probe", result={"accounting_ok": False},
                       control={"reason": "accounting_unavailable"})


def test_acknowledgement_keeps_evidence_and_new_receipts_cumulative(retained):
    log, state, receipt, _ = retained
    original, baseline = log.read_bytes(), state.read_bytes()
    ack = ack_for(log, state)
    assert log.read_bytes() == original and state.read_bytes() == baseline
    log.with_name(ACK_FILE).write_text(json.dumps(ack))
    receipt["id"] = "gen-next"
    with log.open("a") as out:
        out.write(json.dumps(receipt) + "\n")
    snapshot = openrouter_jsonl_snapshot(log, accepted_gaps=load_ack(log, state))
    assert snapshot.total_tokens == 160
    assert snapshot.cache_read_tokens == 80
    assert snapshot.cost_usd == 0.2
    assert snapshot.accounting_incomplete is True
    assert state.read_bytes() == baseline and log.read_bytes().startswith(original)


def test_helper_readonly_preview_and_persisted_ack_emit_partial_provenance(retained):
    log, state, _, _ = retained
    ack = ack_for(log, state)
    cmd = [sys.executable, str(SCRIPTS / "token_usage.py"), "flags", "--state", str(state), "--openrouter-jsonl", str(log)]
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCOREBENCH_")}
    assert subprocess.run(cmd, capture_output=True, env=env).returncode != 0
    before = {p.name: p.read_bytes() for p in log.parent.iterdir()}
    preview = subprocess.run([*cmd, "--openrouter-gap-ack", json.dumps(ack)], capture_output=True, text=True, env=env)
    assert preview.returncode == 0, preview.stderr
    assert "--cost-usd 0.1" in preview.stdout
    assert "--usage-confidence parsed" in preview.stdout
    assert "--tokens-total-source openrouter_usage_partial" in preview.stdout
    assert {p.name: p.read_bytes() for p in log.parent.iterdir()} == before
    log.with_name(ACK_FILE).write_text(json.dumps(ack))
    assert subprocess.run(cmd, capture_output=True, text=True, env=env).stdout == preview.stdout
    start = ["start" if arg == "flags" else arg for arg in cmd]
    baseline = state.read_bytes()
    assert subprocess.run(start, capture_output=True, env=env).returncode != 0
    assert state.read_bytes() == baseline


@pytest.mark.parametrize("change", ["baseline", "receipt", "error", "truncate", "path"])
def test_acknowledged_evidence_tampering_refuses(retained, change):
    log, state, _, _ = retained
    log.with_name(ACK_FILE).write_text(json.dumps(ack_for(log, state)))
    if change == "baseline":
        state.write_text(state.read_text() + " ")
    elif change == "truncate":
        log.write_text("")
    elif change == "path":
        log = log.rename(log.with_name("other.jsonl"))
    else:
        log.write_text(log.read_text().replace("0.1", "0.0") if change == "receipt" else log.read_text().replace("SSLEOFError", "TimeoutError"))
    with pytest.raises(ValueError):
        load_ack(log, state)


def test_new_gap_or_corruption_remains_fatal(retained):
    log, state, _, gap = retained
    log.with_name(ACK_FILE).write_text(json.dumps(ack_for(log, state)))
    prefix = log.read_bytes()
    for extra in (json.dumps(gap) + "\n", "corrupt\n", '{"usage":{"prompt_tokens":1}}\n'):
        log.write_bytes(prefix + extra.encode())
        with pytest.raises(SystemExit):
            openrouter_jsonl_snapshot(log, accepted_gaps=load_ack(log, state))


def test_arbitrary_accounting_errors_cannot_be_acknowledged(retained):
    log, state, _, _ = retained
    for line in ('{"accounting_error":"generation response omitted usage"}\n', 'incomplete'):
        log.write_text(line)
        with pytest.raises(ValueError):
            ack_for(log, state)


def generation_data(**changes):
    return {"id": "gen-missing", "model": "deepseek/deepseek-v4.1-flash", "provider_name": "Together",
            "total_cost": 0, "native_tokens_prompt": 58155, "native_tokens_completion": 731,
            "native_tokens_reasoning": 731, "native_tokens_cached": 0, "finish_reason": None, **changes}


def stream_ack(retained, data=None):
    log, state, receipt, _ = retained
    gap = {"accounting_error": "generation stream omitted final usage", "generation_id": "gen-missing"}
    log.write_text(json.dumps(receipt) + "\n" + json.dumps(gap) + "\n")
    lookup = {"source": "openrouter_generation_api", "fetched_at": 1, "data": data if data is not None else generation_data()}
    ack = prepare_ack(log, state, run_id="run-1", session_id="ses_probe", result={}, control={},
                      model=generation_data()["model"], lookup_generation=lambda *_: lookup)
    return ack


def test_zero_cost_null_finish_lookup_is_partial_not_free_or_double_reasoning(retained):
    log, state, _, _ = retained
    ack = stream_ack(retained)
    before = {path: path.read_bytes() for path in (log, state)}
    snapshot = openrouter_jsonl_snapshot(log, accepted_gaps=load_ack(log, state, preview=json.dumps(ack)))
    assert snapshot.total_tokens == 80 + 58155 + 731
    assert snapshot.output_tokens == 20 + 731
    assert snapshot.cost_usd == 0.1 and snapshot.accounting_incomplete
    assert ack["generation_lookups"]["gen-missing"]["data"]["finish_reason"] is None
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert not log.with_name(ACK_FILE).exists()
    log.with_name(ACK_FILE).write_text(json.dumps(ack))
    # Already-accepted observations must not be fetched afresh and silently revised.
    again = prepare_ack(log, state, run_id="run-1", session_id="ses_probe", result={}, control={},
                        model=generation_data()["model"], lookup_generation=lambda *_: pytest.fail("lookup replay"))
    assert again["generation_lookups"] == ack["generation_lookups"]
    assert again["previous_ack"] == ack


def test_dated_model_alias_in_retained_generation_requires_published_mapping(retained):
    log, state, receipt, _ = retained
    model = generation_data()["model"]
    canonical = model + "-20260910"
    gap = {"accounting_error": "generation stream omitted final usage", "generation_id": "gen-missing", "model": canonical}
    log.write_text(json.dumps(receipt) + "\n" + json.dumps(gap) + "\n")
    lookup = {"source": "openrouter_generation_api", "fetched_at": 1,
              "data": generation_data(model=canonical, native_tokens_prompt=59592,
                                      native_tokens_completion=4394, native_tokens_reasoning=4394),
              "model_alias": {"source": "openrouter_models_api", "fetched_at": 1,
                              "data": {"id": model, "canonical_slug": canonical}}}
    before = {p: p.read_bytes() for p in (log, state)}
    ack = prepare_ack(log, state, run_id="run-1", session_id="ses_probe", result={}, control={},
                      model=model, lookup_generation=lambda *_: lookup)
    snapshot = openrouter_jsonl_snapshot(log, accepted_gaps=ack)
    assert snapshot.total_tokens == 80 + 59592 + 4394
    assert snapshot.cost_usd == 0.1 and snapshot.accounting_incomplete
    assert all(p.read_bytes() == data for p, data in before.items())
    ack["generation_lookups"]["gen-missing"]["model_alias"]["data"]["canonical_slug"] = model + "-other"
    with pytest.raises(ValueError):
        load_ack(log, state, preview=json.dumps(ack))


@pytest.mark.parametrize("change", [{"id": "gen-other"}, {"model": "other/model"}, {"total_cost": None},
    {"total_cost": False}, {"total_cost": -1}, {"total_cost": float("nan")}, {"total_cost": float("inf")},
    {"native_tokens_prompt": None}, {"native_tokens_prompt": 5.5}, {"native_tokens_cached": None},
    {"native_tokens_cached": 58156}, {"native_tokens_reasoning": 732}, {"native_tokens_completion": True},
    {"finish_reason": 0}])
def test_generation_lookup_requires_matching_identity_and_valid_native_usage(retained, change):
    with pytest.raises(ValueError):
        stream_ack(retained, generation_data(**change))


def test_recovered_receipt_is_counted_once_and_conflicts_refuse(retained):
    log, state, _, _ = retained
    ack = stream_ack(retained)
    receipt = {"id": "gen-missing", "usage": generation_usage(generation_data(), "gen-missing", generation_data()["model"])}
    with log.open("a") as out:
        out.write(json.dumps(receipt) + "\n")
    snapshot = openrouter_jsonl_snapshot(log, accepted_gaps=ack)
    assert snapshot.total_tokens == 80 + 58155 + 731
    # A divergent final receipt needs explicit reconciliation, never silent replacement.
    lines = log.read_text().splitlines()
    receipt["usage"]["cost"] = 0.01
    log.write_text("\n".join(lines[:-1]) + "\n" + json.dumps(receipt) + "\n")
    with pytest.raises(SystemExit, match="conflicts"):
        openrouter_jsonl_snapshot(log, accepted_gaps=ack)


def test_stream_gap_requires_captured_generation_id_and_lookup(retained):
    log, state, _, _ = retained
    for identifier in (None, "", "local-id", "gen-../secret", "gen-ok&other=bad"):
        assert not stream_gap({"accounting_error": "generation stream omitted final usage", "generation_id": identifier})
    stream_ack(retained)
    with pytest.raises(ValueError, match="requires an OpenRouter generation lookup"):
        prepare_ack(log, state, run_id="run-1", session_id="ses_probe", result={}, control={}, model=generation_data()["model"])


@pytest.mark.parametrize("error", ["generation stream omitted final usage", "generation stream interrupted before final usage"])
def test_mixed_gaps_fetch_each_generation_once_and_new_gaps_still_stop(retained, error):
    log, state, _, _ = retained
    gap = {"accounting_error": error, "generation_id": "gen-missing", "model": generation_data()["model"]}
    with log.open("a") as out:
        out.write((json.dumps(gap) + "\n") * 2)
    calls = []
    def lookup(identifier, model):
        calls.append((identifier, model))
        return {"source": "openrouter_generation_api", "fetched_at": 1, "data": generation_data(total_cost=0.01)}
    ack = prepare_ack(log, state, run_id="run-1", session_id="ses_probe", result={}, control={},
                      model=generation_data()["model"], lookup_generation=lookup)
    assert len(ack["gaps"]) == 3 and calls == [("gen-missing", generation_data()["model"])]
    snapshot = openrouter_jsonl_snapshot(log, accepted_gaps=ack)
    assert snapshot.total_tokens == 80 + 58155 + 731 and snapshot.cost_usd == 0.11
    with log.open("a") as out:
        out.write(json.dumps({**gap, "generation_id": "gen-another"}) + "\n")
    with pytest.raises(SystemExit, match="incomplete OpenRouter"):
        openrouter_jsonl_snapshot(log, accepted_gaps=load_ack(log, state, preview=json.dumps(ack)))


@pytest.mark.parametrize("change", ["missing_lookup", "extra_lookup", "wrong_source", "changed_model", "wrong_gap"])
def test_stream_ack_validates_lookup_binding_again_on_read(retained, change):
    log, state, _, _ = retained
    ack = stream_ack(retained)
    if change == "missing_lookup":
        ack["generation_lookups"] = {}
    elif change == "extra_lookup":
        ack["generation_lookups"]["gen-other"] = ack["generation_lookups"]["gen-missing"]
    elif change == "wrong_source":
        ack["generation_lookups"]["gen-missing"]["source"] = "manual"
    elif change == "changed_model":
        ack["model"] = "other/model"
    else:
        ack["gaps"] = {"1": "f" * 64}
    with pytest.raises(ValueError):
        load_ack(log, state, preview=json.dumps(ack))


@pytest.mark.parametrize("mode,expected_calls", [("valid", 1), ("eventual", 3), ("unavailable", 3),
    ("unauthorized", 1), ("cooldown", 1), ("redirect", 1), ("invalid", 1)])
def test_generation_lookup_http_is_bounded_readonly_and_does_not_follow_redirects(monkeypatch, mode, expected_calls):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import openrouter_generations

    calls = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append(self.path)
            assert self.headers["Authorization"] == "Bearer local-test-key"
            status = {"unauthorized": 401, "cooldown": 429, "redirect": 302}.get(mode, 200)
            if mode == "unavailable" or (mode == "eventual" and len(calls) < 3):
                status = 404
            body = json.dumps({"data": generation_data(total_cost=None) if mode == "invalid" else generation_data(),
                               "private_extra": "must-not-be-recorded"}).encode()
            self.send_response(status)
            if mode == "redirect":
                self.send_header("Location", "/leaked-key")
            if mode == "cooldown":
                self.send_header("Retry-After", "60")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(openrouter_generations.time, "sleep", lambda _: None)
    try:
        def fetch():
            return lookup_generation("gen-missing", generation_data()["model"],
                upstream=f"http://127.0.0.1:{server.server_port}", api_key="local-test-key")
        if mode in ("valid", "eventual"):
            lookup = fetch()
            assert lookup["data"] == generation_data()
            assert "private_extra" not in json.dumps(lookup) and "local-test-key" not in json.dumps(lookup)
        else:
            with pytest.raises(ValueError):
                fetch()
        assert calls == ["/api/v1/generation?id=gen-missing"] * expected_calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
