import io
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
import urllib.error
import urllib.request

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/scorebench/scripts"
sys.path.insert(0, str(SCRIPTS))
import openrouter_transport as transport
import openrouter_proxy as proxy
import openrouter_agents as agents
import openrouter_generations as generations
import openrouter_run as runner


class TransportTests(unittest.TestCase):
    def test_ipv4_is_default_and_auto_requires_an_explicit_override(self):
        self.assertEqual(transport.configured_ip_family({}), "4")
        for value in ("auto", "4", "6"):
            self.assertEqual(transport.configured_ip_family({transport.IP_FAMILY_ENV: f" {value} "}), value)
        for value in ("", "ipv4", "7", "false", "4;secret"):
            with self.assertRaisesRegex(ValueError, "must be auto, 4 or 6"):
                transport.configured_ip_family({transport.IP_FAMILY_ENV: value})
        original = socket.getaddrinfo
        for cls in (transport._HTTPConnection, transport._HTTPSConnection):
            default = cls("openrouter.ai")
            self.assertIs(default._create_connection.func, transport._create_connection)
            self.assertEqual(default._create_connection.keywords, {"family": socket.AF_INET})
            default.close()
            automatic = cls("openrouter.ai", ip_family="auto")
            self.assertIs(automatic._create_connection, socket.create_connection)
            self.assertIs(socket.getaddrinfo, original)
            automatic.close()

    def test_unset_environment_defaults_to_ipv4_at_the_entrypoint(self):
        env = dict(os.environ)
        env.pop(transport.IP_FAMILY_ENV, None)
        result = subprocess.run([sys.executable, str(SCRIPTS / "openrouter_transport.py"), "--check"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("IP family 4; TLS verification enabled", result.stdout)

    def test_family_selection_is_per_connection_with_no_cross_family_fallback(self):
        for value, family, sockaddr in (("4", socket.AF_INET, ("127.0.0.1", 443)),
                                       ("6", socket.AF_INET6, ("::1", 443, 0, 0))):
            with self.subTest(family=value), mock.patch.object(transport.socket, "getaddrinfo",
                return_value=[(family, socket.SOCK_STREAM, 6, "", sockaddr)]) as lookup, \
                mock.patch.object(transport.socket, "socket") as factory:
                connection = transport._HTTPSConnection("openrouter.ai", ip_family=value)
                sock = connection._create_connection(("openrouter.ai", 443), 7, ("", 0))
                lookup.assert_called_once_with("openrouter.ai", 443, family, socket.SOCK_STREAM)
                factory.assert_called_once_with(family, socket.SOCK_STREAM, 6)
                sock.settimeout.assert_called_once_with(7)
                sock.bind.assert_called_once_with(("", 0))
                sock.connect.assert_called_once_with(sockaddr)
                self.assertEqual(connection.host, "openrouter.ai")
                self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(connection._context.check_hostname)

    def test_failed_connections_close_sockets_and_keep_original_error(self):
        failure = ConnectionRefusedError(111, "refused")
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 443))
                     for host in ("127.0.0.1", "127.0.0.2")]
        with mock.patch.object(transport.socket, "getaddrinfo", return_value=addresses), \
             mock.patch.object(transport.socket, "socket") as factory:
            factory.return_value.connect.side_effect = failure
            with self.assertRaises(ConnectionRefusedError) as raised:
                transport._create_connection(("openrouter.ai", 443), family=socket.AF_INET)
            self.assertIs(raised.exception, failure)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(factory.return_value.close.call_count, 2)
        with mock.patch.object(transport.socket, "getaddrinfo", return_value=[]):
            with self.assertRaisesRegex(OSError, "no addresses"):
                transport._create_connection(("openrouter.ai", 443), family=socket.AF_INET6)

    def test_invalid_setting_fails_preflight_before_network_or_worker_files(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {transport.IP_FAMILY_ENV: "typo", "OPENROUTER_API_KEY": "fake"}), \
             mock.patch.object(runner, "model_metadata") as metadata, \
             mock.patch.object(runner, "check_installation") as installed, \
             mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as raised:
                runner.main(["--harness", "Pi", "--workspace", directory, "--check", "--runtime-control",
                             "--", "pi", "--provider", "openrouter", "--model", "test/model"])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn(transport.IP_FAMILY_ENV, stderr.getvalue())
            metadata.assert_not_called()
            installed.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_standalone_proxy_rejects_invalid_setting_before_creating_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "usage.jsonl"
            result = subprocess.run([sys.executable, str(SCRIPTS / "openrouter_proxy.py"), "--log", str(log)],
                env={**os.environ, transport.IP_FAMILY_ENV: "ipv4", "OPENROUTER_API_KEY": "fake"},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be auto, 4 or 6", result.stderr)
            self.assertNotIn("fake", result.stderr)
            self.assertFalse(log.exists())


class TLSUpstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path.startswith("/api/v1/generation"):
            payload = {"data": {"id": "gen-tls", "model": "test/model", "total_cost": 0.01,
                                "native_tokens_prompt": 100, "native_tokens_completion": 10,
                                "native_tokens_cached": 0, "native_tokens_reasoning": 2,
                                "finish_reason": "stop"}}
        else:
            payload = {"data": {"id": "test/model", "name": "Test", "endpoints": [
                {"supported_parameters": ["tools"], "context_length": 1000,
                 "max_completion_tokens": 100, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]}}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.posts += 1
        if self.server.drop_upload:
            self.rfile.read(1024)
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        self.server.body = self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"id": "gen-tls", "model": "test/model", "usage": {
            "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cost": 0.01,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TLSTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(cls.cert), "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True, timeout=15)
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(str(cls.cert), str(key))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.sni = []
        self.context.set_servername_callback(lambda sock, name, ctx: self.sni.append(name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TLSUpstream)
        self.server.socket = self.context.wrap_socket(self.server.socket, server_side=True)
        self.server.paths, self.server.posts, self.server.drop_upload = [], 0, False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"https://localhost:{self.server.server_port}"
        self.env = mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(self.cert), "NO_PROXY": "*", "no_proxy": "*"})
        self.env.start()
        os.environ.pop(transport.IP_FAMILY_ENV, None)

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_large_post_preserves_body_sni_and_verified_tls(self):
        body = json.dumps({"model": "test/model", "messages": [{"role": "user", "content": "x" * 532027}]}).encode()
        request = urllib.request.Request(self.base + "/api/v1/chat/completions", data=body)
        with proxy.open_upstream(request) as response:
            self.assertEqual(json.load(response)["usage"]["cost"], 0.01)
        self.assertEqual(self.server.body, body)
        self.assertEqual(self.server.posts, 1)
        self.assertEqual(self.sni, ["localhost"])

    def test_model_metadata_and_receipt_recovery_use_the_same_ipv4_transport(self):
        real = transport.socket.getaddrinfo
        with mock.patch.object(transport.socket, "getaddrinfo", wraps=real) as lookup:
            metadata = agents.model_metadata("test/model", self.base)
            receipt = generations.lookup_generation("gen-tls", "test/model", upstream=self.base, api_key="fake-key", require_final=True)
        self.assertEqual(metadata["id"], "test/model")
        self.assertEqual(receipt["data"]["total_cost"], 0.01)
        self.assertEqual(len(lookup.call_args_list), 2)
        for call in lookup.call_args_list:
            self.assertEqual(call.args[2], socket.AF_INET)
        self.assertEqual(self.sni, ["localhost", "localhost"])

    def test_wrong_hostname_and_untrusted_certificates_fail_before_post(self):
        for url, trust in ((self.base.replace("localhost", "127.0.0.1"), str(self.cert)),
                           (self.base, str(Path(self.temporary.name) / "absent-ca.pem"))):
            with self.subTest(url=url), mock.patch.dict(os.environ, {"SSL_CERT_FILE": trust}):
                with self.assertRaises(urllib.error.URLError) as raised:
                    proxy.open_upstream(urllib.request.Request(url, data=b"not sent"))
                self.assertIsInstance(raised.exception.reason, transport.UnsentRequestError)
                self.assertIsInstance(raised.exception.reason.cause, ssl.SSLCertVerificationError)
                self.assertEqual(self.server.posts, 0)

    def test_partial_tls_upload_is_not_retried_or_switched_to_another_family(self):
        self.server.drop_upload = True
        with tempfile.TemporaryDirectory() as directory:
            local = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Handler)
            local.upstream, local.api_key = self.base, "fake-key"
            local.usage_log = proxy.UsageLog(Path(directory) / "usage.jsonl")
            thread = threading.Thread(target=local.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({"model": "test/model", "messages": ["x" * 532027]}).encode()
                request = urllib.request.Request(f"http://127.0.0.1:{local.server_port}/api/v1/chat/completions", data=body)
                for expected in (502, 409):
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(request, timeout=10)
                    self.assertEqual(raised.exception.code, expected)
                    raised.exception.close()
                self.assertEqual(self.server.posts, 1)
                self.assertTrue(local.usage_log.blocked.is_set())
                record = json.loads(local.usage_log.path.read_text())
                self.assertEqual(record["phase"], "request_or_headers")
                self.assertEqual(record["ip_family"], "4")
                self.assertIn("unknown", record["accounting_error"])
                self.assertNotIn("fake-key", local.usage_log.path.read_text())
            finally:
                local.shutdown()
                local.server_close()
                thread.join(5)


if __name__ == "__main__":
    unittest.main()
