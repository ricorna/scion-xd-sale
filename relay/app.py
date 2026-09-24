"""Twilio SMS -> Telegram relay for the Scion xD sale number.

Twilio POSTs inbound SMS to /sms. The request signature is verified with the
Twilio auth token, then the message is forwarded to one Telegram chat.
Replies to the buyer are empty TwiML, so the number never auto-texts back.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import socket
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
PUBLIC_URL = os.environ["PUBLIC_URL"].rstrip("/")  # e.g. https://scion-sms.contextra.io
LABEL = os.environ.get("RELAY_LABEL", "Scion xD")
EMPTY_TWIML = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def twilio_signature_ok(url: str, params: dict, signature: "str | None") -> bool:
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(TWILIO_AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def pretty_number(n: str) -> str:
    d = n[2:] if n.startswith("+1") and len(n) == 12 else None
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d else n


def send_telegram(text: str) -> None:
    body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status != 200:
            raise RuntimeError(f"telegram status {r.status}")


class Handler(BaseHTTPRequestHandler):
    server_version = "scion-sms-relay"

    def log_message(self, format, *args):  # no phone numbers or bodies in logs
        print(f"{self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}", flush=True)

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b'{"ok":true}', "application/json")
        self._send(404, b"not found")

    def do_POST(self):
        if self.path.split("?")[0] != "/sms":
            return self._send(404, b"not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 100_000:
            return self._send(413, b"too large")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        if not twilio_signature_ok(PUBLIC_URL + self.path, params, self.headers.get("X-Twilio-Signature")):
            return self._send(403, b"bad signature")

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
