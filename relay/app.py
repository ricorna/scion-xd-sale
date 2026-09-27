"""Signed Twilio SMS and end-of-call notifications to Telegram."""
import base64
from contextlib import closing
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import socket
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_ID", "").strip()  # topic/thread in a topics-enabled chat
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")  # e.g. https://scion-sms.contextra.io
LABEL = os.environ.get("RELAY_LABEL", "Scion xD")
CALL_NUMBER = os.environ.get("CALL_NUMBER", "+19313406265")
CALL_DB_PATH = os.environ.get("CALL_DB_PATH", "/data/calls.sqlite3")
RELEASE_SHA = os.environ.get("RELEASE_SHA", "unknown")
TERMINAL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
EMPTY_TWIML = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def twilio_signature_ok(url: str, params: dict, signature: "str | None") -> bool:
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(TWILIO_AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest), (signature or "").encode())


def pretty_number(n: str) -> str:
    d = n[2:] if n.startswith("+1") and len(n) == 12 else None
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d else n


def send_telegram(text: str) -> int:
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if TELEGRAM_THREAD_ID:
        payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status != 200:
            raise RuntimeError("telegram HTTP failure")
        result = json.loads(r.read())
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise RuntimeError("telegram API failure")
        receipt = result.get("result")
        message_id = receipt.get("message_id") if isinstance(receipt, dict) else None
        if type(message_id) is not int or message_id <= 0:
            raise RuntimeError("telegram receipt missing")
        return message_id


def call_db():
    # Connections are per request; close explicitly (sqlite's context only commits).
    db = sqlite3.connect(CALL_DB_PATH, timeout=2)
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS calls (
            call_sid TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK (state IN ('reserved', 'sent', 'unknown')),
            message_id INTEGER,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
    except Exception:
        db.close()
        raise
    return db


def reserve_call(sid):
    with closing(call_db()) as db, db:
        # Commit BEFORE touching Telegram; this permanent reservation has no TTL.
        inserted = db.execute("INSERT OR IGNORE INTO calls (call_sid, state) VALUES (?, 'reserved')", (sid,)).rowcount
    return inserted == 1


def record_delivery(sid, state, message_id=None):
    with closing(call_db()) as db, db:
        db.execute("UPDATE calls SET state = ?, message_id = ? WHERE call_sid = ?",
                   (state, message_id, sid))


def call_text(params):
    sender = params.get("From", "")
    callback = sender if re.fullmatch(r"\+[1-9][0-9]{1,14}", sender) else None
    display = pretty_number(callback) if callback else "withheld/unavailable"
    where = ", ".join(x for x in (params.get("FromCity", "").title(), params.get("FromState", "")) if x)
    lines = [f"☎️ <b>{html.escape(LABEL)} inbound call ended</b>",
             f"From: <b>{html.escape(display)}</b>" + (f" ({html.escape(where)})" if where else ""),
             f"Status: {html.escape(params['CallStatus'])}"]
    if params.get("CallerName") == "SYNTHETIC TEST":
        lines.insert(0, "<b>SYNTHETIC TEST</b>")
    if callback:
        lines += ["", f"Callback: <code>{html.escape(callback)}</code>"]
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    server_version = "scion-sms-relay"

    def log_message(self, format, *args):
        # Base parser/error messages can contain arbitrary unauthenticated text.
        pass

    def log_request(self, code="-", size="-"):
        route = getattr(self, "path", "").split("?")[0]
        route = route if route in {"/sms", "/calls", "/health"} else "other"
        status = int(code) if isinstance(code, int) and 100 <= code <= 599 else 0
        print(f"request {route} -> {status}", flush=True)

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            try:
                with closing(call_db()) as db, db:
                    # Check write readiness too, without reserving a real CallSid.
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("SELECT call_sid, state, message_id FROM calls LIMIT 1")
                ready = True
            except sqlite3.Error:
                ready = False
            body = json.dumps({"ok": ready, "release_sha": RELEASE_SHA}).encode()
            return self._send(200 if ready else 503, body, "application/json")
        self._send(404, b"not found")

    def do_POST(self):
        route = self.path.split("?")[0]
        if route not in {"/sms", "/calls"}:
            return self._send(404, b"not found")
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,10}", lengths[0])):
            return self._send(400, b"bad content length")
        length = int(lengths[0])
        if length > 100_000:
            return self._send(413, b"too large")
        try:
            self.connection.settimeout(10)
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self._send(400, b"incomplete body")
            pairs = urllib.parse.parse_qsl(raw.decode("utf-8"), keep_blank_values=True,
                                           errors="strict", max_num_fields=1000)
        except (ValueError, TimeoutError, OSError):
            return self._send(400, b"bad form")
        params = dict(pairs)
        if len(params) != len(pairs):
            return self._send(400, b"duplicate form keys")
        signatures = self.headers.get_all("X-Twilio-Signature", [])
        if len(signatures) != 1 or not twilio_signature_ok(PUBLIC_URL + self.path, params, signatures[0]):
            return self._send(403, b"bad signature")
        if route == "/calls":
            return self._call(params)
        return self._sms(params)

    def _call(self, params):
        if params.get("Direction") != "inbound" or params.get("CallStatus") not in TERMINAL_STATUSES:
            return self._send(200, EMPTY_TWIML, "text/xml")
        sid = params.get("CallSid", "")
        if not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid) or params.get("To") != CALL_NUMBER:
            return self._send(400, b"invalid call")
        text = call_text(params)
        try:
            reserved = reserve_call(sid)
        except sqlite3.Error:
            print("call reservation failed: database unavailable", flush=True)
            return self._send(503, b"database unavailable")
        if not reserved:
            return self._send(200, EMPTY_TWIML, "text/xml")
        try:
            message_id = send_telegram(text)
        except Exception as exc:
            # Never log exception text: URLs, credentials and phone data may be in it.
            print(f"call delivery unknown: {type(exc).__name__}; manual reconciliation required", flush=True)
            try:
                record_delivery(sid, "unknown")
            except sqlite3.Error:
                print("call delivery state write failed; reservation retained", flush=True)
            return self._send(200, EMPTY_TWIML, "text/xml")
        synthetic = params.get("CallerName") == "SYNTHETIC TEST"
        print(f"call delivery sent message_id={message_id} synthetic={str(synthetic).lower()}", flush=True)
        try:
            record_delivery(sid, "sent", message_id)
        except sqlite3.Error:
            # The committed reservation still prevents a second delivery.
            print("call receipt write failed; reservation retained; manual reconciliation required", flush=True)
        return self._send(200, EMPTY_TWIML, "text/xml")

    def _sms(self, params):
        sender = params.get("From", "unknown")
        body = params.get("Body", "")
        media = [params[f"MediaUrl{i}"] for i in range(int(params.get("NumMedia", "0") or 0))
                 if params.get(f"MediaUrl{i}")]
        where = ", ".join(x for x in (params.get("FromCity", "").title(), params.get("FromState", "")) if x)
        lines = [f"🚗 <b>{html.escape(LABEL)} text</b> from <b>{html.escape(pretty_number(sender))}</b>"
                 + (f" ({html.escape(where)})" if where else ""),
                 "", html.escape(body) or "<i>(no text)</i>"]
        lines += [f'📎 <a href="{html.escape(u)}">photo/attachment {i + 1}</a>' for i, u in enumerate(media)]
        lines += ["", f"Reply from your phone: <code>{html.escape(sender)}</code>"]
        try:
            send_telegram("\n".join(lines))
        except Exception as e:  # let Twilio log a failure so it is visible in the console
            print(f"telegram delivery failed: {type(e).__name__}", flush=True)
            return self._send(502, b"relay failed")
        self._send(200, EMPTY_TWIML, "text/xml")


class DualStackServer(ThreadingHTTPServer):
    """Listen on IPv6 and IPv4 so health checks against `localhost` (::1) succeed."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


if __name__ == "__main__":
    DualStackServer(("::", 8080), Handler).serve_forever()
