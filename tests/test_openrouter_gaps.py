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
