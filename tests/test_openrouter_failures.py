"""No inference spend: real local proxy, injected upstream outcomes."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/scorebench/scripts"
sys.path.insert(0, str(SCRIPTS))
import openrouter_proxy as proxy
from openrouter_failures import POLICY, SOURCE, accounting_summary, response_evidence, retry_delay
from openrouter_journal import append_record, read_records, reconcile_requests
from token_usage import openrouter_jsonl_snapshot


class FailurePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "usage.jsonl"
        self.log = proxy.UsageLog(self.path)
        self.log.enable_experiment_policy()
        self.server = mock.Mock(usage_log=self.log, upstream="https://example.invalid", api_key="secret",
                                allowed_models={"vendor/model"}, stopping=False, runtime_control_path=None)
        self.handler = object.__new__(proxy.Handler)
        self.handler.server, self.handler.command, self.handler.path = self.server, "POST", "/api/v1/chat/completions"
        self.handler.request_model, self.handler.request_attempt = "vendor/model", 1
        self.handler.wfile = io.BytesIO()
        self.handler.send_response = mock.Mock()
        self.handler.send_header = mock.Mock()
        self.handler.end_headers = mock.Mock()

    def response(self, status=502, body=b"Bad Gateway", headers=None):
        response = io.BytesIO(body)
        response.status = status
        response.headers = {"Content-Type": "text/plain", **(headers or {})}
        return response

    def relay(self, status, body, headers=None):
        self.handler.request_id = self.log.journal.begin("vendor/model", b"private prompt", 1)
        self.handler._relay(self.response(status, body, headers), record_usage=True)

    def forward(self, responses):
        body = b'{"model":"vendor/model","messages":[{"content":"secret prompt"}]}'
        self.handler.headers = {"Content-Length": str(len(body))}
        self.handler.rfile = io.BytesIO(body)
        with mock.patch.object(proxy, "open_upstream", side_effect=responses) as sent, \
                mock.patch.object(proxy.time, "sleep") as slept:
            self.handler._forward()
        return sent, slept

    def test_gateway_failure_is_exact_experiment_exclusion_not_a_free_invoice(self):
        self.relay(502, b"Bad Gateway secret upstream body")
        self.assertFalse(self.log.blocked.is_set())
        snapshot = openrouter_jsonl_snapshot(self.path)
        self.assertEqual((snapshot.cost_usd, snapshot.total_tokens), (0, 0))
        self.assertTrue(snapshot.experiment_accounting)
        self.assertFalse(snapshot.accounting_incomplete)
        summary = accounting_summary(read_records(self.path))
        self.assertEqual(summary["accounting_policy"], POLICY)
        self.assertEqual(summary["excluded_infrastructure_requests"], 1)
        self.assertEqual(summary["excluded_provider_cost_unknown_requests"], 1)
        self.assertNotIn("secret", self.path.read_text())
        self.assertEqual(self.log.journal.rows()[0]["state"], "excluded")
        self.assertTrue(reconcile_requests(self.path, mock.Mock(), check=True, abandoned=True)["accounting_complete"])
        self.assertFalse(proxy.UsageLog(self.path).blocked.is_set())

    def test_known_failed_cost_is_preserved_but_not_charged_to_experiment(self):
        self.relay(503, json.dumps({"id": "gen-failed", "error": {"code": 503},
            "usage": {"prompt_tokens": 100, "completion_tokens": 9, "cost": .5}}).encode())
        self.assertEqual(openrouter_jsonl_snapshot(self.path).cost_usd, 0)
        summary = accounting_summary(read_records(self.path))
        self.assertEqual(summary["excluded_provider_cost_usd"], .5)
        self.assertEqual(summary["excluded_provider_cost_unknown_requests"], 0)

    def test_gateway_retry_then_success_preserves_request_ids_and_one_receipt(self):
        good = json.dumps({"id": "gen-ok", "model": "vendor/model", "usage": {
            "prompt_tokens": 10, "completion_tokens": 2, "cost": .03}}).encode()
        responses = [HTTPError("https://example.invalid", 502, "Bad Gateway", {}, io.BytesIO(b"Bad Gateway")),
                     self.response(503, b"Unavailable", {"Retry-After": "0"}), self.response(200, good)]
        sent, slept = self.forward(responses)
        self.assertEqual(sent.call_count, 3)
        self.assertEqual(len({call.args[0].data for call in sent.call_args_list}), 1)
        self.assertEqual(slept.call_count, 2)
        self.handler.send_response.assert_called_once_with(200)
        self.assertEqual(openrouter_jsonl_snapshot(self.path).cost_usd, .03)
        rows = self.log.journal.rows()
        self.assertEqual([row["state"] for row in rows], ["excluded", "excluded", "accounted"])
        self.assertEqual(len({row["request_id"] for row in rows}), 3)

    def test_retries_exhaust_without_corrupting_accounting(self):
        sent, _ = self.forward([self.response() for _ in range(3)])
        self.assertEqual(sent.call_count, 3)
        self.handler.send_response.assert_called_once_with(502)
        self.assertFalse(self.log.blocked.is_set())
        self.assertEqual(openrouter_jsonl_snapshot(self.path).cost_usd, 0)

    def test_all_classified_http_failures_can_be_excluded_without_omitting_audit(self):
        for status in (408, 500, 502, 503, 504):
            self.relay(status, b'{"error":{"message":"unavailable"},"choices":null}')
        self.assertEqual(accounting_summary(read_records(self.path))["excluded_infrastructure_requests"], 5)
        self.assertFalse(openrouter_jsonl_snapshot(self.path).accounting_incomplete)
        diagnostics = [json.loads(line) for line in self.path.with_name("http-errors.jsonl").read_text().splitlines()]
        self.assertEqual([row["http_status"] for row in diagnostics], [408, 500, 502, 503, 504])

    def test_stop_during_backoff_prevents_another_request(self):
        def stop(_):
            self.server.stopping = True
        body = b'{"model":"vendor/model"}'
        self.handler.headers = {"Content-Length": str(len(body))}
        self.handler.rfile = io.BytesIO(body)
        with mock.patch.object(proxy, "open_upstream", return_value=self.response()) as sent, \
                mock.patch.object(proxy.time, "sleep", side_effect=stop):
            self.handler._forward()
        self.assertEqual(sent.call_count, 1)
        self.handler.send_response.assert_called_once_with(409)

    def test_explicit_json_gateway_error_with_http_200_is_excluded(self):
        self.relay(200, b'{"error":{"code":502,"message":"provider down"}}')
        self.assertFalse(self.log.blocked.is_set())
        self.assertEqual(accounting_summary(read_records(self.path))["excluded_infrastructure_requests"], 1)

    def test_unknown_missing_receipt_is_not_reclassified_as_gateway(self):
        self.relay(200, b"unexpected response")
        self.assertTrue(self.log.blocked.is_set())
        with self.assertRaises(SystemExit):
            openrouter_jsonl_snapshot(self.path)
        evidence = read_records(self.path)[-1][2]["evidence"]
        self.assertEqual(evidence["http_status"], 200)

    def test_typed_errors_across_api_skins_and_lossy_status_codes(self):
        for obj in (
            {"error": {"metadata": {"error_type": "provider_unavailable"}}},
            {"error": {"type": "api_error", "error_type": "server"}},
            {"type": "response.failed", "response": {"id": "resp-failed", "error_type": "provider_overloaded",
             "error": {"code": "server_error"}}},
        ):
            with self.subTest(obj=obj):
                self.relay(200, json.dumps(obj).encode())
                self.assertFalse(self.log.blocked.is_set())
        self.assertEqual(accounting_summary(read_records(self.path))["excluded_infrastructure_requests"], 3)
        for error_type in ("authentication", "payment_required", "max_tokens_exceeded", "refusal", "unmapped"):
            obj = {"error": {"code": "server_error"}, "error_type": error_type}
            code, _, _ = response_evidence(500, "application/json", json.dumps(obj).encode())
            self.assertNotIn(code, {408, 500, 502, 503, 504})

    def test_partial_model_work_is_never_excluded_or_replayed(self):
        for field in ("content", "reasoning", "tool_calls"):
            with self.subTest(field=field):
                body = json.dumps({"error": {"code": 502}, "choices": [{"message": {field: "work"}}]}).encode()
                status, evidence, _ = response_evidence(502, "application/json", body)
                self.assertTrue(evidence["model_output"])
        self.relay(502, b'{"choices":[{"message":{"reasoning":"useful work"}}]}')
        self.assertTrue(self.log.blocked.is_set())
        with self.assertRaises(SystemExit):
            openrouter_jsonl_snapshot(self.path)

    def test_rate_limit_retries_and_permanent_errors_do_not(self):
        sent, slept = self.forward([self.response(429, b"rate limit", {"Retry-After": "0"}),
                                   self.response(401, b"auth required")])
        self.assertEqual(sent.call_count, 2)
        self.assertEqual(slept.call_count, 1)
        self.handler.send_response.assert_called_once_with(401)
        self.assertFalse(self.log.blocked.is_set())
        self.assertEqual([row["state"] for row in self.log.journal.rows()], ["excluded", "rejected"])

    def test_retry_after_is_never_shortened_or_unbounded(self):
        self.assertEqual(retry_delay(429, {"Retry-After": "15"}, 1), 15)
        self.assertIsNone(retry_delay(429, {"Retry-After": "300"}, 1))
        self.assertIsNone(retry_delay(503, {"Retry-After": "nan"}, 1))
        self.assertIsNone(retry_delay(503, {}, 3))
        for code in (400, 401, 402, 403, 404, 422):
            self.assertIsNone(retry_delay(code, {}, 1))

    def test_stream_gateway_before_output_is_excluded_but_partial_work_is_not(self):
        self.handler.request_id = self.log.journal.begin("vendor/model", b"prompt", 1)
        stream = b'data: {"error":{"code":502,"message":"provider failed"}}\n\ndata: [DONE]\n\n'
        self.handler._pump_stream(io.BytesIO(stream), record_usage=True)
        self.assertFalse(self.log.blocked.is_set())
        self.assertEqual(accounting_summary(read_records(self.path))["excluded_infrastructure_requests"], 1)
        self.handler.request_id = self.log.journal.begin("vendor/model", b"prompt", 1)
        with_output = b'data: {"choices":[{"delta":{"reasoning":"thinking"}}]}\n\n' + stream
        self.handler._pump_stream(io.BytesIO(with_output), record_usage=True)
        self.assertTrue(self.log.blocked.is_set())
        with self.assertRaises(SystemExit):
            openrouter_jsonl_snapshot(self.path)

    def test_crash_between_exclusion_append_and_journal_commit_is_recoverable(self):
        self.relay(502, b"Bad Gateway")
        request = self.log.journal.rows()[0]
        self.log.journal.finish(request["request_id"], "pending")
        before = self.path.read_bytes()
        result = reconcile_requests(self.path, mock.Mock(), check=True, abandoned=True)
        self.assertTrue(result["accounting_complete"])
        self.assertEqual(self.path.read_bytes(), before)
        reconcile_requests(self.path, mock.Mock(), abandoned=True)
        self.assertEqual(self.log.journal.rows()[0]["state"], "excluded")

    def test_conflicting_exclusion_cannot_hide_a_successful_receipt(self):
        self.relay(502, b'{"id":"gen-failed","error":{"code":502}}')
        append_record(self.path, {"id": "gen-failed", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": .1}})
        with self.assertRaises(SystemExit):
            openrouter_jsonl_snapshot(self.path)


if __name__ == "__main__":
    unittest.main()
