"""HTTP integration tests; RELAY_TEST_TRANSPORT=memory needs no socket binding."""
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
from email.message import Message
import hashlib
import hmac
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

os.environ.update(TWILIO_AUTH_TOKEN="test-auth", TELEGRAM_BOT_TOKEN="test-bot",
                  TELEGRAM_CHAT_ID="test-chat", PUBLIC_URL="https://relay.example")
spec = importlib.util.spec_from_file_location("relay_app", Path(__file__).parents[1] / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class Response:
    status = 200

    def __init__(self, body=None):
        self.body = body if body is not None else b'{"ok":true,"result":{"message_id":123}}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.body


class MemorySocket:
    """Wire-format HTTP through the real handler, without an OS listener."""
    def __init__(self):
        self.request = bytearray()

    def sendall(self, data):
        self.request.extend(data)

    def makefile(self, mode):
        response = io.BytesIO()
        class HandlerSocket:
            def settimeout(inner, timeout):
                pass

            def makefile(inner, mode, *args):
                return io.BytesIO(self.request) if mode == "rb" else response

            def sendall(inner, data):
                response.write(data)
        app.Handler(HandlerSocket(), ("127.0.0.1", 0), None)
        return io.BytesIO(response.getvalue())

    def close(self):
        pass


class MemoryConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = MemorySocket()


class RelayHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = patch.multiple(app, CALL_DB_PATH=str(Path(self.tmp.name) / "calls.sqlite3"),
                                     CALL_NUMBER="+19313406265", RELEASE_SHA="test-release", create=True)
        self.config.start()
        self.messages = []
        self.outbound = patch.object(app.urllib.request, "urlopen", side_effect=self.telegram)
        self.outbound.start()
        self.logs = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.logs)
        self.capture.__enter__()
        self.start_server()
        self.params = dict(CallSid="CA" + "a" * 32, Direction="inbound", CallStatus="completed",
                           To="+19313406265", From="+16155551234")

    def start_server(self):
        if os.environ.get("RELAY_TEST_TRANSPORT") == "memory":
            self.server = None
            return
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        if self.server is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def tearDown(self):
        self.stop_server()
        self.capture.__exit__(None, None, None)
        self.outbound.stop()
        self.config.stop()
        self.tmp.cleanup()

    def telegram(self, request, timeout):
        self.messages.append(json.loads(request.data))
        return Response()

    def request(self, params=None, path="/calls", signature=None, raw=None, headers=None, method="POST"):
        params = self.params if params is None else params
        body = urllib.parse.urlencode(params) if raw is None else raw
        signing = app.PUBLIC_URL + path + "".join(k + params[k] for k in sorted(params))
        sig = base64.b64encode(hmac.new(b"test-auth", signing.encode(), hashlib.sha1).digest()).decode()
        fields = {"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": sig if signature is None else signature}
        fields.update(headers or {})
        conn = (http.client.HTTPConnection(*self.server.server_address, timeout=3)
                if self.server else MemoryConnection("memory", timeout=3))
        try:
            conn.request(method, path, body=body, headers=fields)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def assert_fresh_process_dedupes(self):
        # A brand-new interpreter has no access to this process's globals/locks.
        script = """
import json, sys
from unittest.mock import patch
import test_app
app = test_app.app
app.CALL_DB_PATH, body, signature = json.loads(sys.stdin.read())
with patch.object(app, 'send_telegram') as send:
    conn = test_app.MemoryConnection('memory')
    conn.request('POST', '/calls', body, {'X-Twilio-Signature': signature})
    response = conn.getresponse()
    assert response.status == 200, response.status
    response.read()
    send.assert_not_called()
"""
        params = self.params
        signing = app.PUBLIC_URL + "/calls" + "".join(k + params[k] for k in sorted(params))
        sig = base64.b64encode(hmac.new(b"test-auth", signing.encode(), hashlib.sha1).digest()).decode()
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parent,
                                input=json.dumps([app.CALL_DB_PATH, urllib.parse.urlencode(params), sig]),
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_call_notification_and_receipt(self):
        self.assertEqual(self.request(), (200, app.EMPTY_TWIML))
        self.assertEqual(len(self.messages), 1)
        text = self.messages[0]["text"]
        self.assertIn("(615) 555-1234", text)
        self.assertIn("<code>+16155551234</code>", text)
        self.assertIn("completed", text)
        with contextlib.closing(sqlite3.connect(app.CALL_DB_PATH)) as db, db:
            self.assertEqual(db.execute("SELECT state, message_id FROM calls").fetchone(), ("sent", 123))
        self.assertIn("message_id=123", self.logs.getvalue())
        self.assertNotIn(self.params["From"], self.logs.getvalue())

    def test_all_terminal_statuses(self):
        for i, status in enumerate(("completed", "busy", "failed", "no-answer", "canceled")):
            with self.subTest(status=status):
                self.assertEqual(self.request(dict(self.params, CallStatus=status, CallSid="CA" + f"{i:032x}"))[0], 200)
        self.assertEqual(len(self.messages), 5)

    def test_non_inbound_and_non_terminal_are_ignored(self):
        for changes in ({"Direction": "outbound-api"}, {"Direction": ""}, {"CallStatus": "ringing"}, {"CallStatus": "answered"}, {"CallStatus": ""}):
            self.assertEqual(self.request(dict(self.params, **changes))[0], 200)
        self.assertEqual(self.messages, [])

    def test_invalid_sid_and_wrong_recipient_rejected(self):
        for sid in ("", "CA123", "CB" + "a" * 32, "CA" + "z" * 32, "CA" + "a" * 32 + "\n"):
            self.assertEqual(self.request(dict(self.params, CallSid=sid))[0], 400)
        self.assertEqual(self.request(dict(self.params, To="+19999999999"))[0], 400)
        self.assertEqual(self.messages, [])

    def test_configurable_recipient(self):
        with patch.object(app, "CALL_NUMBER", "+12025550100"):
            self.assertEqual(self.request(dict(self.params, To="+12025550100"))[0], 200)
        self.assertEqual(len(self.messages), 1)

    def test_signature_required_including_synthetic(self):
        self.assertEqual(self.request(signature="wrong")[0], 403)
        self.assertEqual(self.request(dict(self.params, CallerName="SYNTHETIC TEST"), signature="")[0], 403)
        self.assertEqual(self.messages, [])

    def test_signature_includes_query_string(self):
        self.assertEqual(self.request(path="/calls?test=1")[0], 200)
        self.assertEqual(len(self.messages), 1)

    def test_duplicate_keys_rejected_on_both_endpoints(self):
        for path in ("/sms", "/calls"):
            for suffix in ("&From=other", "&%46rom=other", "&From="):
                self.assertEqual(self.request(path=path, raw=urllib.parse.urlencode(self.params) + suffix)[0], 400)
        self.assertEqual(self.messages, [])

    def test_malformed_lengths(self):
        for path in ("/sms", "/calls"):
            for value, expected in (("-1", 400), ("abc", 400), ("1.5", 400), ("100001", 413)):
                with self.subTest(path=path, value=value):
                    self.assertEqual(self.request(path=path, raw="", headers={"Content-Length": value})[0], expected)
        self.assertEqual(self.messages, [])

    def test_transfer_encoding_rejected(self):
        self.assertEqual(self.request(raw="", headers={"Transfer-Encoding": "chunked", "Content-Length": "0"})[0], 400)

    def test_duplicate_and_restart(self):
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(self.request()[0], 200)
        self.stop_server()
        self.start_server()
        self.assertEqual(self.request(dict(self.params, CallStatus="failed"))[0], 200)
        self.assert_fresh_process_dedupes()
        self.assertEqual(len(self.messages), 1)

    def test_concurrent_callbacks_and_no_write_lock_during_network(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(request, timeout):
            # A separate writer must be able to acquire the DB while sending.
            with contextlib.closing(sqlite3.connect(app.CALL_DB_PATH, timeout=0.1)) as db, db:
                db.execute("BEGIN IMMEDIATE")
            entered.set()
            self.assertTrue(release.wait(3))
            return self.telegram(request, timeout)
        with patch.object(app.urllib.request, "urlopen", side_effect=blocked), ThreadPoolExecutor(max_workers=6) as pool:
            first = pool.submit(self.request)
            try:
                self.assertTrue(entered.wait(3))
                others = [pool.submit(self.request) for _ in range(5)]
                self.assertEqual([f.result()[0] for f in others], [200] * 5)
            finally:
                release.set()
            self.assertEqual(first.result()[0], 200)
        self.assertEqual(len(self.messages), 1)

    def test_uncertain_delivery_is_never_retried(self):
        def accepted_then_disconnected(request, timeout):
            self.messages.append(json.loads(request.data))
            raise OSError("secret test-bot +16155551234")
        with patch.object(app.urllib.request, "urlopen", side_effect=accepted_then_disconnected):
            self.assertEqual(self.request(dict(self.params, CallerName="SYNTHETIC TEST"))[0], 200)
        self.stop_server()
        self.start_server()
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.messages), 1)
        self.assert_fresh_process_dedupes()
        self.assertIn("delivery unknown", self.logs.getvalue())
        self.assertNotIn("test-bot", self.logs.getvalue())
        self.assertNotIn("+16155551234", self.logs.getvalue())
        self.assertNotIn("message_id=", self.logs.getvalue())
        self.assertNotIn("synthetic=", self.logs.getvalue())

    def test_crash_after_reservation_suppresses_delivery(self):
        self.assertTrue(app.reserve_call(self.params["CallSid"]))
        self.assertEqual(self.request()[0], 200)
        self.assert_fresh_process_dedupes()
        self.assertEqual(self.messages, [])

    def test_receipt_write_failure_keeps_reservation(self):
        with patch.object(app, "record_delivery", side_effect=sqlite3.OperationalError("secret")):
            self.assertEqual(self.request()[0], 200)
        self.assert_fresh_process_dedupes()
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("receipt write failed", self.logs.getvalue())
        self.assertNotIn("secret", self.logs.getvalue())

    def test_telegram_http_error_is_safe_and_not_retried(self):
        error = urllib.error.HTTPError("https://secret", 400, "secret +16155551234", Message(), io.BytesIO())
        with patch.object(app.urllib.request, "urlopen", side_effect=error) as outbound:
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(outbound.call_count, 1)
        error.close()
        self.assertNotIn("secret", self.logs.getvalue())
        self.assertNotIn("+16155551234", self.logs.getvalue())

    def test_raw_malformed_framing(self):
        for headers, body in ((b"", b""), (b"Content-Length: 0\r\nContent-Length: 0\r\n", b""),
                              (b"Content-Length: 5\r\n", b"x"),
                              (b"Content-Length: 1\r\n", b"\xff")):
            wire = MemorySocket()
            wire.sendall(b"POST /calls HTTP/1.0\r\n" + headers + b"\r\n" + body)
            response = http.client.HTTPResponse(wire)
            response.begin()
            self.assertEqual(response.status, 400)
            response.close()
        self.assertEqual(self.messages, [])

    def test_bad_telegram_responses_have_no_receipt_or_retry(self):
        for i, body in enumerate((b'not json', b'{"ok":false,"description":"secret"}', b'{"ok":true,"result":{}}', b'{"ok":true,"result":{"message_id":true}}')):
            params = dict(self.params, CallSid="CA" + f"{i:032x}")
            with patch.object(app.urllib.request, "urlopen", return_value=Response(body)) as outbound:
                self.assertEqual(self.request(params)[0], 200)
                self.assertEqual(self.request(params)[0], 200)
                self.assertEqual(outbound.call_count, 1)
        self.assertNotIn("message_id=", self.logs.getvalue())
        self.assertNotIn("secret", self.logs.getvalue())

    def test_safe_html_geography_and_synthetic_name(self):
        with patch.object(app, "LABEL", "<Sale & car>"):
            self.assertEqual(self.request(dict(self.params, CallerName="SYNTHETIC TEST", FromCity="<town>", FromState="A&B"))[0], 200)
        text = self.messages[0]["text"]
        self.assertIn("SYNTHETIC TEST", text)
        self.assertIn("&lt;Sale &amp; car&gt;", text)
        self.assertIn("&lt;Town&gt;, A&amp;B", text)
        self.assertIn("synthetic=true", self.logs.getvalue())

    def test_unavailable_callers_and_untrusted_names(self):
        for i, caller in enumerate(("anonymous", "", "<script>", "6155551234", "+0123456", "+1１２３４５６", "+16155551234\n")):
            self.assertEqual(self.request(dict(self.params, CallSid="CA" + f"{i:032x}", From=caller, CallerName="Untrusted Name"))[0], 200)
        for message in self.messages:
            text = message["text"]
            self.assertIn("withheld/unavailable", text)
            self.assertNotIn("<code>", text)
            self.assertNotIn("Callback:", text)
            self.assertNotIn("Untrusted Name", text)
            self.assertNotIn("<script>", text)

    def test_international_callback(self):
        self.assertEqual(self.request(dict(self.params, From="+442079460123"))[0], 200)
        self.assertIn("<code>+442079460123</code>", self.messages[0]["text"])

    def test_health_release_and_db_readiness(self):
        status, body = self.request(path="/health", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True, "release_sha": "test-release"})
        with patch.object(app, "CALL_DB_PATH", str(Path(self.tmp.name) / "missing" / "db")):
            status, body = self.request(path="/health", method="GET")
            self.assertEqual(status, 503)
            self.assertFalse(json.loads(body)["ok"])

    def test_db_failure_before_reservation_can_retry(self):
        with patch.object(app, "CALL_DB_PATH", str(Path(self.tmp.name) / "missing" / "db")):
            self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.messages, [])
        self.assertEqual(self.request()[0], 200)
        self.assertEqual(len(self.messages), 1)

    def test_sms_preserved(self):
        params = dict(From="+16155551234", Body="hi <there>", NumMedia="1", MediaUrl0="https://example.com/a?x=1&y=2", FromCity="nashville", FromState="TN")
        status, body = self.request(params, path="/sms")
        self.assertEqual((status, body), (200, app.EMPTY_TWIML))
        text = self.messages[0]["text"]
        self.assertIn("hi &lt;there&gt;", text)
        self.assertIn("Nashville, TN", text)
        self.assertIn("photo/attachment 1", text)
        self.assertIn("Reply from your phone: <code>+16155551234</code>", text)
        self.assertEqual(self.request(params, path="/sms", signature="bad")[0], 403)

    def test_sms_telegram_failure_still_returns_502(self):
        with patch.object(app.urllib.request, "urlopen", side_effect=OSError("secret")):
            self.assertEqual(self.request(dict(From="+16155551234", Body="hello"), path="/sms")[0], 502)
        self.assertNotIn("secret", self.logs.getvalue())

    def test_telegram_thread_payload(self):
        with patch.object(app, "TELEGRAM_THREAD_ID", "83051"):
            self.assertEqual(app.send_telegram("thread test"), 123)
        self.assertEqual(self.messages[0]["message_thread_id"], 83051)
        self.assertEqual(self.messages[0]["chat_id"], "test-chat")

    def test_untrusted_http_errors_never_enter_logs(self):
        for request in (
            b"PRIVATE_TOKEN_abc /health HTTP/1.0\r\n\r\n",
            b"GET /PRIVATE_PATH_abc HTTP/invalid_PRIVATE_VERSION\r\n\r\n",
            b"GET /PRIVATE_PATH_abc HTTP/1.0\r\n\r\n",
        ):
            wire = MemorySocket()
            wire.sendall(request)
            self.assertTrue(wire.makefile("rb").read())
        self.assertNotIn("PRIVATE_", self.logs.getvalue())

    def test_send_telegram_returns_message_id(self):
        self.assertEqual(app.send_telegram("test"), 123)


if __name__ == "__main__":
    unittest.main()
