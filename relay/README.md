# Scion SMS and inbound call relay

`POST /sms` forwards texts and attachments to Telegram and returns empty TwiML,
without texting the sender. `POST /calls` sends a notification **after an inbound
call ends**, not when it rings. It accepts only `Direction=inbound` and terminal
`CallStatus` values `completed`, `busy`, `failed`, `no-answer`, and `canceled`.
Other directions/statuses are acknowledged without sending. Terminal inbound
callbacks require a `CA` plus 32 hexadecimal digit `CallSid` and matching `To`.

The inspected sale number is `+19313406265`. Its current VoiceUrl points to an
existing TwiML Bin and **must remain exactly unchanged**, including its method
and other voice settings. There is no voice endpoint or wrapper in this app.
The number's StatusCallback was empty. After deployment, configure only its
number-level StatusCallback to `PUBLIC_URL/calls`, method **POST**. This code
change does not update Twilio, deploy, or send a live test.

Both POST endpoints require the Twilio HMAC-SHA1 signature using the same auth
token, the externally visible `PUBLIC_URL` plus request path/query, and all form
fields. Duplicate decoded form keys and malformed request framing are rejected
before signature verification. Reverse proxies must preserve the path/query.

## Configuration and durable storage

Required: `TWILIO_AUTH_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and
`PUBLIC_URL` (public relay origin, without a trailing slash). Optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_THREAD_ID` | empty | Telegram topic |
| `RELAY_LABEL` | `Scion xD` | Escaped notification heading |
| `CALL_NUMBER` | `+19313406265` | Exact expected callback `To` |
| `CALL_DB_PATH` | `/data/calls.sqlite3` | Durable SQLite deduplication database |
| `RELEASE_SHA` | `unknown` | Release identifier in `/health` |

Build with `relay/` as the Docker context. The relay Dockerfile accepts Coolify's
`SOURCE_COMMIT` build argument and sets `RELEASE_SHA`. `/health` returns JSON
containing `ok` and `release_sha`, with HTTP 503 if SQLite cannot be opened,
initialized, read, or write-locked. Readiness cannot prove a volume is persistent.

**Before deployment, mount a durable volume at `/data`, writable by the nonroot
`relay` user.** The image creates `/data` with that ownership; a bind mount can
override it, so check the mounted directory's permissions. Preserve the database
across restarts, image upgrades, and rollbacks. All serving instances must use
the same SQLite file on a filesystem with reliable SQLite locking; separate
replica volumes do not deduplicate each other. No TTL or automatic cleanup is
applied. Do not reset the database to retry delivery.

## At-most-once delivery and reconciliation

An atomic `INSERT OR IGNORE` permanently reserves each CallSid and commits
**before** Telegram is called. Network I/O holds no SQLite write lock. Concurrent,
repeated, and post-restart callbacks see the reservation and return 200 without
sending again. Successful `sendMessage` must return valid JSON `ok: true` and a
positive integer `result.message_id`; that receipt is stored with state `sent`.

This deliberately favors at-most-once automatic sending over guaranteed delivery.
Telegram can accept a message and disconnect before returning a receipt. Any
send error, invalid response, or missing receipt is treated as delivery unknown:
the reservation remains, the app attempts to record `unknown`, logs only a safe
exception class, and returns 200. Even a definite Telegram rejection is not
automatically retried. A crash after reservation but before sending can lose a
notification; a crash after sending can leave it `reserved`. Receipt-write
failure also retains the reservation. A database failure before reservation
returns 503 so the callback can be retried without having sent anything.

For manual reconciliation:

1. Inspect `calls` in the durable database (`call_sid`, `state`, `message_id`,
   `created_at`). Review `reserved` or `unknown` rows and receipt-write failures.
2. Match the CallSid/time against Twilio call records and inspect the Telegram
   chat around that time. A `sent` row has a message ID; success logs also contain
   the ID and `synthetic=true/false`. Logs omit caller numbers, bodies, tokens,
   exception messages, and CallSids.
3. If delivery is uncertain, leave the reservation intact. Only after a human
   resolves the ambiguity should they manually send any missing notification.
   Record that reconciliation separately; do not delete a row or replay it to
   trigger an automatic resend.

Caller numbers are displayed prettily; the copyable callback is strictly E.164
(`+`, nonzero first digit, up to 15 ASCII digits). Non-E.164 callers display
`withheld/unavailable` and have no callback. Geography appears only from supplied
`FromCity`/`FromState`; no lookup or inference occurs. All reflected text is HTML
escaped. Arbitrary caller names are ignored.

## Synthetic testing

The tests use dummy credentials and intercept Telegram; no credentials or live
services are needed. For a separately authorized live synthetic test, POST a
normally signed form to `/calls` with a fresh `CA` + 32 hexadecimal digit CallSid,
`Direction=inbound`, `CallStatus=completed`, the configured `To`, an appropriate
test `From`, and **exactly** `CallerName=SYNTHETIC TEST`. The Telegram message is
prominently labeled `SYNTHETIC TEST`. This exact caller-name value is the only one
reflected; it does not bypass authentication, validation, or deduplication. Use
the configured secrets through your normal secret handling, never in source or
logs. Replaying the same CallSid must produce no second notification.

## Local checks and CI

From the repository root, using only Python's standard library:

```sh
python3 -m unittest discover -s relay/tests -v
python3 -m py_compile relay/app.py relay/tests/test_app.py
```

The default tests use a real local TCP HTTP server. In environments that prohibit
binding sockets, run `RELAY_TEST_TRANSPORT=memory python3 -m unittest discover -s
relay/tests -v` (on one line). This transports serialized HTTP through the real
handler and HTTP response parser in memory, with real SQLite and concurrent
requests; fresh-interpreter checks verify permanent reservations across process
restarts. It does not verify TCP listening. Telegram is mocked in both modes.

GitHub CI runs the TCP suite and compilation on Python 3.12. Its PR DCO check
requires author `Signed-off-by` trailers for non-merge human commits; use
`git commit -s` when you are ready to commit and can make the DCO certification.
It excludes GitHub bot commits and does not change historical commits. No commit
is created by these checks.
