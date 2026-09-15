import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/scorebench/scripts"
sys.path.insert(0, str(SCRIPTS))
from openrouter_journal import RequestJournal, append_record, read_records, reconcile_requests
from openrouter_generations import generation_usage
from openrouter_proxy import UsageLog
from openrouter_reconcile import main
from token_usage import openrouter_jsonl_snapshot

MODEL = "deepseek/test"
DATA = {"id": "gen-test", "model": MODEL, "total_cost": 0.1, "native_tokens_prompt": 100,
        "native_tokens_completion": 20, "native_tokens_cached": 30, "native_tokens_reasoning": 5,
        "finish_reason": "stop"}
LOOKUP = {"source": "openrouter_generation_api", "data": DATA, "fetched_at": 1}


@pytest.fixture
def log(tmp_path):
    return UsageLog(tmp_path / ".scorebench/openrouter/usage.jsonl")


def begin(log, generation_id="gen-test"):
    request_id = log.journal.begin(MODEL, b'{"prompt":"private prompt"}', 1)
    if generation_id:
        log.journal.observe(request_id, {"id": generation_id, "model": MODEL, "usage": {"completion_tokens": 2},
                                         "content": "secret response"})
    return request_id


def gap(log):
    request_id = begin(log)
    log.error("generation stream omitted final usage", request_id=request_id, generation_id="gen-test", model=MODEL)
    return request_id


def test_pending_request_is_durable_and_secret_free(log):
    request_id = begin(log)
    journal = RequestJournal(log.path, readonly=True)
    row = journal.rows()[0]
    assert row["request_id"] == request_id
    assert row["generation_id"] == "gen-test" and row["state"] == "pending"
    assert "private prompt" not in journal.path.read_bytes().decode(errors="ignore")
    assert "secret response" not in journal.path.read_bytes().decode(errors="ignore")
    assert journal.path.stat().st_mode & 0o777 == 0o600
    assert log.path.stat().st_mode & 0o777 == 0o600


def test_metadata_is_not_persisted(log):
    request_id = begin(log)
    log.journal.observe(request_id, {"usage": {"api_key": "secret-key", "service_tier": "private", "cost": 0.1}})
    row = log.journal.rows()[0]
    assert row["generation_id"] == "gen-test"
    assert "secret-key" not in row["observed"] and "private" not in row["observed"]
    assert json.loads(row["observed"])["id"] == "gen-test"


def test_append_only_repair_and_repeat_query_deduplicate(log):
    gap(log)
    original = log.path.read_bytes()
    with pytest.raises(SystemExit):
        openrouter_jsonl_snapshot(log.path)
    lookup = Mock(return_value=LOOKUP)
    result = log.reconcile(lookup)
    assert result["reconciled"] == 1 and result["accounting_complete"]
    assert log.path.read_bytes().startswith(original)
    assert not log.blocked.is_set()
    snapshot = openrouter_jsonl_snapshot(log.path)
    assert snapshot.cost_usd == 0.1 and snapshot.output_tokens == 20
    repaired = log.path.read_bytes()
    assert log.reconcile(lookup)["reconciled"] == 0
    assert log.path.read_bytes() == repaired
    assert lookup.call_count == 1


def test_process_death_after_generation_id_is_reconciled(tmp_path):
    path = tmp_path / "usage.jsonl"
    code = """import os,sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from openrouter_proxy import UsageLog
log=UsageLog(Path(sys.argv[2]))
request=log.journal.begin('deepseek/test',b'private',1)
log.journal.observe(request,{'id':'gen-test','model':'deepseek/test'})
os._exit(17)
"""
    run = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(path)], check=False)
    assert run.returncode == 17
    result = reconcile_requests(path, Mock(return_value=LOOKUP), abandoned=True)
    assert result["reconciled"] == 1 and result["accounting_complete"]
    records = read_records(path)
    assert records[0][2]["accounting_error"] == "retained request ended without a durable receipt"
    assert records[1][2]["resolved_errors"]
    assert openrouter_jsonl_snapshot(path).cost_usd == 0.1


def test_process_death_after_receipt_before_journal_commit(log):
    request_id = begin(log)
    append_record(log.path, {"id": "gen-test", "model": MODEL, "request_id": request_id,
                             "usage": generation_usage(DATA, "gen-test", MODEL)})
    lookup = Mock()
    assert reconcile_requests(log.path, lookup, abandoned=True)["accounting_complete"]
    lookup.assert_not_called()
    assert len(read_records(log.path)) == 1 and log.journal.rows()[0]["state"] == "accounted"


def test_unknown_request_has_no_provider_id_and_is_not_free(log):
    begin(log, None)
    lookup = Mock()
    result = reconcile_requests(log.path, lookup, abandoned=True)
    assert result["without_generation_id"] == 1 and not result["accounting_complete"]
    lookup.assert_not_called()
    with pytest.raises(SystemExit):
        openrouter_jsonl_snapshot(log.path)


def test_pending_lookup_backoff_survives_restart(log):
    gap(log)
    lookup = Mock(side_effect=ValueError("not settled"))
    assert not log.reconcile(lookup)["accounting_complete"]
    reopened = UsageLog(log.path)
    assert not reopened.reconcile(lookup)["accounting_complete"]
    assert lookup.call_count == 1
    row = log.journal.rows()[0]
    with patch("openrouter_journal.time.time", return_value=row["next_lookup_at"] + 1):
        result = reopened.reconcile(Mock(return_value=LOOKUP))
    assert result["accounting_complete"] and not reopened.blocked.is_set()


@pytest.mark.parametrize("field,value", [("id", "gen-other"), ("model", "other/model"), ("total_cost", None),
                                         ("native_tokens_prompt", None), ("finish_reason", None)])
def test_inconclusive_or_mismatched_provider_evidence_cannot_repair(log, field, value):
    gap(log)
    lookup = copy.deepcopy(LOOKUP)
    lookup["data"][field] = value
    assert not log.reconcile(Mock(return_value=lookup))["accounting_complete"]
    assert len(read_records(log.path)) == 1
    with pytest.raises(SystemExit):
        openrouter_jsonl_snapshot(log.path)


def test_corrupt_original_error_invalidates_repair(log):
    gap(log)
    log.reconcile(Mock(return_value=LOOKUP))
    records = log.path.read_text().splitlines()
    error = json.loads(records[0])
    error["model"] = "different/model"
    log.path.write_text(json.dumps(error) + "\n" + records[1] + "\n")
    with pytest.raises(SystemExit, match="reconciliation"):
        openrouter_jsonl_snapshot(log.path)


def test_independent_unidentified_error_stays_unresolved(log):
    gap(log)
    log.error("upstream transport failure; generation acceptance and cost are unknown", request_id="other")
    result = log.reconcile(Mock(return_value=LOOKUP))
    assert result["reconciled"] == 1 and not result["accounting_complete"] and log.blocked.is_set()


def test_read_only_cli_does_not_change_evidence(log, monkeypatch):
    gap(log)
    lock_path = Path(str(log.path) + ".lock")
    lock_path.touch()
    before = {path: path.read_bytes() for path in log.path.parent.iterdir() if path.is_file()}
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")
    with patch("openrouter_reconcile.lookup_generation", return_value=LOOKUP) as lookup:
        assert main(["--workspace", str(log.path.parents[2]), "--check"]) == 0
    assert lookup.call_count == 1
    assert before == {path: path.read_bytes() for path in log.path.parent.iterdir() if path.is_file()}


def test_cli_refuses_live_writer(log, monkeypatch):
    gap(log)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only")
    with open(str(log.path) + ".lock", "a") as lock, patch("openrouter_reconcile.lookup_generation") as lookup:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert main(["--workspace", str(log.path.parents[2])]) == 2
        lookup.assert_not_called()


def test_legacy_stream_gap_without_journal_can_be_repaired(log):
    log.error("generation stream omitted final usage", generation_id="gen-test", model=MODEL)
    log.journal.path.unlink()
    result = reconcile_requests(log.path, Mock(return_value=LOOKUP))
    assert result["accounting_complete"]
    assert openrouter_jsonl_snapshot(log.path).cost_usd == 0.1


def test_active_requests_are_not_abandoned_or_queried(log):
    begin(log)
    log.request_started()
    lookup = Mock()
    assert log.reconcile(lookup, abandoned=True) is None
    log.request_finished()
    lookup.assert_not_called()
    assert log.journal.rows()[0]["state"] == "pending"


def test_unfinished_ledger_is_not_appended(log):
    gap(log)
    with log.path.open("a") as handle:
        handle.write('{"usage":')
    original = log.path.read_bytes()
    with pytest.raises(ValueError, match="unfinished"):
        reconcile_requests(log.path, Mock(return_value=LOOKUP))
    assert log.path.read_bytes() == original


def test_journal_run_binding_cannot_be_changed(log):
    start = log.path.parent.parent / "supervisor-run-start.json"
    start.write_text(json.dumps({"run": {"run_id": "different"}}))
    with pytest.raises(ValueError, match="match"):
        RequestJournal(log.path)


def test_bounded_lookup_batch(log):
    for i in range(7):
        request_id = begin(log, f"gen-{i}")
        log.journal.finish(request_id, "unresolved")
    lookup = Mock(side_effect=ValueError())
    report = reconcile_requests(log.path, lookup)
    assert report["lookups"] == lookup.call_count == 4
    assert report["unresolved"] == 7


def test_conflicting_stream_ids_never_auto_reconcile(log):
    request_id = begin(log)
    log.journal.observe(request_id, {"id": "gen-changed", "model": MODEL})
    lookup = Mock(return_value=LOOKUP)
    report = reconcile_requests(log.path, lookup, abandoned=True)
    assert not report["accounting_complete"]
    assert log.journal.rows()[0]["state"] == "conflict"
    lookup.assert_not_called()


def test_repair_duplicate_provenance_counts_once(log):
    gap(log)
    log.reconcile(Mock(return_value=LOOKUP))
    repair = read_records(log.path)[1][2]
    repair["reconciliation"]["fetched_at"] = 2
    append_record(log.path, repair)
    assert openrouter_jsonl_snapshot(log.path).cost_usd == 0.1
    repair["usage"]["cost"] = 0.2
    append_record(log.path, repair)
    with pytest.raises(SystemExit):
        openrouter_jsonl_snapshot(log.path)


def test_many_concurrent_request_journals_have_unique_durable_ids(log):
    def record(index):
        request_id = log.journal.begin(MODEL, str(index).encode(), 1)
        response = {"id": f"gen-{index}", "model": MODEL, "request_id": request_id,
                    "usage": generation_usage({**DATA, "id": f"gen-{index}"}, f"gen-{index}", MODEL)}
        log.journal.observe(request_id, response)
        log.record(response)
        return request_id
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(record, range(120)))
    assert len(set(ids)) == 120
    assert len(log.journal.rows()) == 120
    assert all(row["state"] == "accounted" for row in log.journal.rows())
    assert openrouter_jsonl_snapshot(log.path).cost_usd == 12.0
    lookup = Mock()
    assert log.reconcile(lookup)["accounting_complete"]
    lookup.assert_not_called()


@pytest.mark.parametrize("counter", [float("nan"), -1, "secret-not-a-counter", True])
def test_invalid_observed_counters_cannot_be_silently_repaired(log, counter):
    request_id = begin(log)
    log.journal.observe(request_id, {"usage": {"completion_tokens": counter}})
    lookup = Mock(return_value=LOOKUP)
    assert not reconcile_requests(log.path, lookup, abandoned=True)["accounting_complete"]
    assert log.journal.rows()[0]["state"] == "conflict"
    lookup.assert_not_called()


def test_missing_committed_ledger_receipt_cannot_be_silently_skipped(log):
    request_id = begin(log)
    log.journal.finish(request_id, "accounted")
    report = reconcile_requests(log.path, Mock(side_effect=ValueError()), abandoned=True)
    assert not report["accounting_complete"]
    with pytest.raises(SystemExit):
        openrouter_jsonl_snapshot(log.path)
