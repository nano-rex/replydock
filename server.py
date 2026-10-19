#!/usr/bin/env python3
"""ReplyDock: a tiny self-hosted Slack and WhatsApp auto-reply hub."""
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
CHANNELS = ("both", "slack", "whatsapp")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript", ".svg": "image/svg+xml", ".ico": "image/x-icon"}
SLACK_MAX_AGE = 300


def load_dotenv(path):
    """Load KEY=VALUE lines from a .env file without overriding the real environment."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_dotenv(os.getenv("REPLYDOCK_ENV_FILE", ROOT / ".env"))


def env(name, default=""):
    return os.getenv(name, default).strip()


def db_path():
    return env("REPLYDOCK_DB") or str(ROOT / "replydock.sqlite3")


def connect():
    connection = sqlite3.connect(db_path(), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
    connection = connect()
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phrase TEXT NOT NULL,
        reply TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'both',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        sender TEXT NOT NULL,
        text TEXT NOT NULL,
        reply TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL
    )""")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(messages)")}
    if "status" not in columns:
        connection.execute("ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT ''")
    connection.execute("""CREATE TABLE IF NOT EXISTS seen_events (
        key TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL
    )""")
    if connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0:
        connection.executemany(
            "INSERT INTO rules (phrase, reply, channel, created_at) VALUES (?, ?, ?, ?)",
            [
                ("hello", "Hello! This is an automatic reply.", "both", int(time.time())),
                ("help", "Please describe what you need and a person will follow up.", "both", int(time.time())),
            ],
        )
    connection.commit()
    connection.close()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode()


def request_json(url, payload, headers=None):
    request = urllib.request.Request(url, data=json_bytes(payload), method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RuntimeError(f"remote API returned {error.code}: {body[:300]}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"remote API unreachable: {error.reason}") from error


def hmac_sha256(secret, message):
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def whatsapp_signature_ok(secret, body, provided):
    if not secret:
        return True
    return bool(provided) and hmac.compare_digest("sha256=" + hmac_sha256(secret, body), provided)


def slack_signature_ok(secret, body, timestamp, provided, now=None):
    """Slack signs "v0:<timestamp>:<body>" and rejects requests older than five minutes."""
    if not secret:
        return True
    if not timestamp or not provided:
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > SLACK_MAX_AGE:
            return False
    except ValueError:
        return False
    expected = "v0=" + hmac_sha256(secret, b"v0:" + timestamp.encode() + b":" + body)
    return hmac.compare_digest(expected, provided)


def rule_matches(phrase, text):
    if phrase.startswith("re:"):
        try:
            return re.search(phrase[3:], text, re.IGNORECASE) is not None
        except re.error:
            return False
    return phrase.casefold() in text.casefold()


def matching_rule(channel, text):
    connection = connect()
    rows = connection.execute("SELECT * FROM rules WHERE enabled = 1 ORDER BY id").fetchall()
    connection.close()
    for row in rows:
        if row["channel"] in ("both", channel) and rule_matches(row["phrase"], text):
            return row
    return None


def first_seen(key):
    """Record an event id; returns False if it was already processed (webhook retries)."""
    connection = connect()
    try:
        connection.execute("INSERT INTO seen_events (key, created_at) VALUES (?, ?)", (key, int(time.time())))
        connection.execute("DELETE FROM seen_events WHERE created_at < ?", (int(time.time()) - 7 * 86400,))
        connection.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        connection.close()


def save_message(channel, sender, text, reply="", status=""):
    connection = connect()
    connection.execute(
        "INSERT INTO messages (channel, sender, text, reply, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (channel, sender[:200], text[:10000], reply[:10000], status[:500], int(time.time())),
    )
    connection.commit()
    connection.close()


def send_slack(channel, text, thread_ts=""):
    token = env("SLACK_BOT_TOKEN")
    if not token or token == "xoxb-your-token":
        raise RuntimeError("SLACK_BOT_TOKEN is not configured")
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    result = request_json("https://slack.com/api/chat.postMessage", payload, {"Authorization": f"Bearer {token}"})
    if not result.get("ok"):
        raise RuntimeError(f"Slack error: {result.get('error', 'message rejected')}")


def send_whatsapp(recipient, text):
    token = env("WHATSAPP_ACCESS_TOKEN")
    phone_id = env("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_id:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID is not configured")
    url = f"https://graph.facebook.com/{env('WHATSAPP_GRAPH_VERSION', 'v20.0')}/{phone_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": recipient, "type": "text", "text": {"body": text}}
    request_json(url, payload, {"Authorization": f"Bearer {token}"})


def send_whatsapp_template(recipient, name="hello_world", language="en_US"):
    """Business-initiated messages must use an approved template; Meta's test apps ship with hello_world."""
    token = env("WHATSAPP_ACCESS_TOKEN")
    phone_id = env("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_id:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID is not configured")
    url = f"https://graph.facebook.com/{env('WHATSAPP_GRAPH_VERSION', 'v20.0')}/{phone_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": recipient, "type": "template", "template": {"name": name, "language": {"code": language}}}
    return request_json(url, payload, {"Authorization": f"Bearer {token}"})


def process_message(channel, sender, text, metadata):
    rule = matching_rule(channel, text)
    if not rule:
        save_message(channel, sender, text, "", "no match")
        return ""
    reply = rule["reply"]
    try:
        if channel == "slack":
            send_slack(metadata["channel"], reply, metadata.get("thread_ts", ""))
        elif channel == "whatsapp":
            send_whatsapp(metadata["recipient"], reply)
        status = "sent"
    except RuntimeError as error:
        print(f"reply failed: {error}", flush=True)
        status = f"failed: {error}"
    save_message(channel, sender, text, reply, status)
    return reply


def process_in_background(channel, items):
    """Webhooks must be acknowledged quickly (Slack allows 3 seconds), so replies are sent off-thread."""
    def run():
        for item in items:
            try:
                process_message(channel, *item)
            except Exception as error:
                print(f"processing failed: {error}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def parse_slack_event(payload):
    event = payload.get("event", {})
    if payload.get("type") != "event_callback" or event.get("type") != "message":
        return None
    if event.get("bot_id") or event.get("subtype") or not event.get("text") or not event.get("user"):
        return None
    event_id = payload.get("event_id") or f"{event.get('channel')}:{event.get('ts')}"
    if not first_seen(f"slack:{event_id}"):
        return None
    thread_ts = event.get("thread_ts", "") if env("SLACK_REPLY_IN_THREAD", "1") == "0" else event.get("thread_ts", event.get("ts", ""))
    return event["user"], event["text"], {"channel": event.get("channel", ""), "thread_ts": thread_ts}


def parse_whatsapp_messages(payload):
    items = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                if message.get("type") != "text" or not message.get("text", {}).get("body"):
                    continue
                if message.get("id") and not first_seen(f"whatsapp:{message['id']}"):
                    continue
                items.append((message.get("from", "unknown"), message["text"]["body"], {"recipient": message.get("from", "")}))
    return items


def integration_status():
    slack_token = env("SLACK_BOT_TOKEN")
    return {
        "slack": {"token": bool(slack_token) and slack_token != "xoxb-your-token", "signing_secret": bool(env("SLACK_SIGNING_SECRET"))},
        "whatsapp": {
            "access_token": bool(env("WHATSAPP_ACCESS_TOKEN")),
            "phone_number_id": bool(env("WHATSAPP_PHONE_NUMBER_ID")),
            "verify_token": bool(env("WHATSAPP_VERIFY_TOKEN")),
            "app_secret": bool(env("WHATSAPP_APP_SECRET")),
        },
        "admin_auth": bool(env("REPLYDOCK_ADMIN_TOKEN")),
    }


def clean_rule(payload):
    phrase, reply = str(payload.get("phrase", "")).strip(), str(payload.get("reply", "")).strip()
    channel = payload.get("channel") if payload.get("channel") in CHANNELS else "both"
    if not phrase or not reply:
        raise ValueError("phrase and reply are required")
    if phrase.startswith("re:"):
        try:
            re.compile(phrase[3:])
        except re.error as error:
            raise ValueError(f"invalid regular expression: {error}") from error
    return phrase, reply, channel


class Handler(BaseHTTPRequestHandler):
    server_version = "ReplyDock/0.2"

    def log_message(self, format, *args):
        print(f"{self.address_string()} {format % args}", flush=True)

    def send_json(self, value, status=HTTPStatus.OK):
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=HTTPStatus.OK):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length > 1_000_000:
            raise ValueError("request too large")
        return self.rfile.read(length)

    def authorized(self):
        """The dashboard and /api/* use HTTP Basic auth (any username) when REPLYDOCK_ADMIN_TOKEN is set."""
        token = env("REPLYDOCK_ADMIN_TOKEN")
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                password = base64.b64decode(header[6:]).decode().split(":", 1)[1]
            except (ValueError, IndexError):
                password = ""
            if hmac.compare_digest(password.encode(), token.encode()):
                return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="ReplyDock"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def rule_id(self, path):
        match = re.fullmatch(r"/api/rules/(\d+)(/toggle)?", path)
        return (int(match.group(1)), bool(match.group(2))) if match else (None, False)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_json({"ok": True}); return
        if parsed.path == "/webhooks/whatsapp":
            query = parse_qs(parsed.query)
            expected = env("WHATSAPP_VERIFY_TOKEN")
            if query.get("hub.mode", [""])[0] == "subscribe" and expected and hmac.compare_digest(query.get("hub.verify_token", [""])[0], expected):
                self.send_text(query.get("hub.challenge", [""])[0]); return
            self.send_json({"error": "verification failed"}, HTTPStatus.FORBIDDEN); return
        if not self.authorized():
            return
        if parsed.path == "/api/rules":
            connection = connect(); rows = [dict(row) for row in connection.execute("SELECT * FROM rules ORDER BY id")]; connection.close()
            self.send_json(rows); return
        if parsed.path == "/api/messages":
            connection = connect(); rows = [dict(row) for row in connection.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 100")]; connection.close()
            self.send_json(rows); return
        if parsed.path == "/api/status":
            self.send_json(integration_status()); return
        path = "/index.html" if parsed.path in ("", "/") else parsed.path
        static = (ROOT / "static").resolve()
        file = (static / path.lstrip("/")).resolve()
        if not file.is_relative_to(static) or not file.is_file():
            self.send_error(404); return
        data = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(file.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self.read_body()
            if parsed.path == "/webhooks/slack":
                if not slack_signature_ok(env("SLACK_SIGNING_SECRET"), body, self.headers.get("X-Slack-Request-Timestamp", ""), self.headers.get("X-Slack-Signature", "")):
                    self.send_json({"error": "invalid signature"}, HTTPStatus.UNAUTHORIZED); return
                payload = json.loads(body or b"{}")
                if payload.get("type") == "url_verification":
                    self.send_json({"challenge": payload.get("challenge", "")}); return
                item = parse_slack_event(payload)
                if item:
                    process_in_background("slack", [item])
                self.send_json({"ok": True}); return
            if parsed.path == "/webhooks/whatsapp":
                if not whatsapp_signature_ok(env("WHATSAPP_APP_SECRET"), body, self.headers.get("X-Hub-Signature-256", "")):
                    self.send_json({"error": "invalid signature"}, HTTPStatus.UNAUTHORIZED); return
                items = parse_whatsapp_messages(json.loads(body or b"{}"))
                if items:
                    process_in_background("whatsapp", items)
                self.send_json({"ok": True}); return
            if not self.authorized():
                return
            payload = json.loads(body or b"{}")
            if parsed.path == "/api/rules":
                phrase, reply, channel = clean_rule(payload)
                connection = connect()
                cursor = connection.execute("INSERT INTO rules (phrase, reply, channel, created_at) VALUES (?, ?, ?, ?)", (phrase, reply, channel, int(time.time())))
                connection.commit(); connection.close()
                self.send_json({"ok": True, "id": cursor.lastrowid}); return
            if parsed.path == "/api/test":
                channel = payload.get("channel") if payload.get("channel") in ("slack", "whatsapp") else "slack"
                rule = matching_rule(channel, str(payload.get("text", "")))
                self.send_json({"matched": bool(rule), "rule_id": rule["id"] if rule else None, "reply": rule["reply"] if rule else ""}); return
            if parsed.path == "/api/whatsapp/test":
                recipient = re.sub(r"\D", "", str(payload.get("to", "")))
                if not 8 <= len(recipient) <= 15:
                    raise ValueError("enter the phone number with country code, e.g. +60123456789")
                try:
                    result = send_whatsapp_template(recipient)
                except RuntimeError as error:
                    self.send_json({"ok": False, "error": str(error)}, HTTPStatus.BAD_GATEWAY); return
                save_message("whatsapp", recipient, "(test template hello_world)", "hello_world", "sent")
                self.send_json({"ok": True, "message_id": (result.get("messages") or [{}])[0].get("id", "")}); return
            rule_id, toggle = self.rule_id(parsed.path)
            if rule_id is not None and toggle:
                connection = connect()
                cursor = connection.execute("UPDATE rules SET enabled = 1 - enabled WHERE id = ?", (rule_id,))
                connection.commit(); connection.close()
                self.send_json({"ok": bool(cursor.rowcount)}, HTTPStatus.OK if cursor.rowcount else HTTPStatus.NOT_FOUND); return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            print(f"request failed: {error}", flush=True)
            self.send_json({"error": "internal error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self):
        if not self.authorized():
            return
        rule_id, toggle = self.rule_id(urlparse(self.path).path)
        if rule_id is None or toggle:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND); return
        connection = connect()
        cursor = connection.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        connection.commit(); connection.close()
        self.send_json({"ok": bool(cursor.rowcount)}, HTTPStatus.OK if cursor.rowcount else HTTPStatus.NOT_FOUND)


def main():
    init_db()
    host, port = env("REPLYDOCK_HOST", "127.0.0.1"), int(env("REPLYDOCK_PORT", "8080"))
    if host not in ("127.0.0.1", "localhost", "::1") and not env("REPLYDOCK_ADMIN_TOKEN"):
        print("warning: dashboard is reachable without a password; set REPLYDOCK_ADMIN_TOKEN", flush=True)
    print(f"ReplyDock listening on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
