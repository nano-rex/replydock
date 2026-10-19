#!/usr/bin/env python3
"""ReplyDock: a tiny self-hosted Slack and WhatsApp auto-reply hub."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
DB_PATH = os.getenv("REPLYDOCK_DB", str(ROOT / "replydock.sqlite3"))
HOST = os.getenv("REPLYDOCK_HOST", "127.0.0.1")
PORT = int(os.getenv("REPLYDOCK_PORT", "8080"))
WA_GRAPH_VERSION = os.getenv("WHATSAPP_GRAPH_VERSION", "v20.0")


def env(name):
    return os.getenv(name, "").strip()


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
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
    if connection.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0:
        connection.executemany(
            "INSERT INTO rules (phrase, reply, channel, created_at) VALUES (?, ?, ?, ?)",
            [
                ("hello", "Hello! This is an automatic reply.", "both", int(time.time())),
                ("help", "Please describe what you need and a person will follow up.", "both", int(time.time())),
            ],
        )
    connection.commit()
    return connection


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False).encode()


def request_json(url, payload, headers=None):
    request = urllib.request.Request(url, data=json_bytes(payload), method="POST")
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        raise RuntimeError(f"remote API returned {error.code}: {body[:300]}") from error


def signature_ok(secret, body, provided, prefix="sha256="):
    if not secret:
        return True
    expected = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(provided) and hmac.compare_digest(expected, provided)


def matching_rule(channel, text):
    connection = db()
    rows = connection.execute("SELECT * FROM rules WHERE enabled = 1 ORDER BY id").fetchall()
    connection.close()
    lowered = text.casefold()
    for row in rows:
        if row["channel"] not in ("both", channel):
            continue
        try:
            matched = re.search(row["phrase"], text, re.IGNORECASE) if row["phrase"].startswith("re:") else row["phrase"].casefold() in lowered
        except re.error:
            matched = False
        if matched:
            return row["reply"]
    return ""


def save_message(channel, sender, text, reply=""):
    connection = db()
    connection.execute(
        "INSERT INTO messages (channel, sender, text, reply, created_at) VALUES (?, ?, ?, ?, ?)",
        (channel, sender[:200], text[:10000], reply[:10000], int(time.time())),
    )
    connection.commit()
    connection.close()


def send_slack(channel, text, thread_ts=""):
    token = env("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not configured")
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    result = request_json("https://slack.com/api/chat.postMessage", payload, {"Authorization": f"Bearer {token}"})
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "Slack rejected the message"))


def send_whatsapp(recipient, text):
    token = env("WHATSAPP_ACCESS_TOKEN")
    phone_id = env("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_id:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN or WHATSAPP_PHONE_NUMBER_ID is not configured")
    url = f"https://graph.facebook.com/{WA_GRAPH_VERSION}/{phone_id}/messages"
    request_json(url, {"messaging_product": "whatsapp", "to": recipient, "type": "text", "text": {"body": text}}, {"Authorization": f"Bearer {token}"})


def process_message(channel, sender, text, metadata):
    reply = matching_rule(channel, text)
    save_message(channel, sender, text, reply)
    if not reply:
        return ""
    try:
        if channel == "slack":
            send_slack(metadata["channel"], reply, metadata.get("thread_ts", ""))
        elif channel == "whatsapp":
            send_whatsapp(metadata["recipient"], reply)
    except RuntimeError as error:
        print(f"reply failed: {error}", flush=True)
    return reply


def parse_slack_event(payload):
    event = payload.get("event", {})
    if payload.get("type") != "event_callback" or event.get("type") != "message":
        return None
    if event.get("bot_id") or event.get("subtype") or not event.get("text"):
        return None
    return event.get("user", "unknown"), event["text"], {"channel": event.get("channel", ""), "thread_ts": event.get("thread_ts", event.get("ts", ""))}


def parse_whatsapp_messages(payload):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                if message.get("type") == "text" and message.get("text", {}).get("body"):
                    yield message.get("from", "unknown"), message["text"]["body"], {"recipient": message.get("from", "")}


def read_body(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if length > 1_000_000:
        raise ValueError("request too large")
    return handler.rfile.read(length)


class Handler(BaseHTTPRequestHandler):
    server_version = "ReplyDock/0.1"

    def log_message(self, format, *args):
        print(f"{self.address_string()} {format % args}", flush=True)

    def send_json(self, value, status=HTTPStatus.OK):
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_json({"ok": True})
            return
        if parsed.path == "/webhooks/whatsapp":
            query = parse_qs(parsed.query)
            if query.get("hub.verify_token", [""])[0] == env("WHATSAPP_VERIFY_TOKEN"):
                self.send_response(200); self.end_headers(); self.wfile.write(query.get("hub.challenge", [""])[0].encode()); return
            self.send_json({"error": "verification failed"}, HTTPStatus.FORBIDDEN); return
        if parsed.path == "/api/rules":
            connection = db(); rows = [dict(row) for row in connection.execute("SELECT * FROM rules ORDER BY id")]; connection.close(); self.send_json(rows); return
        if parsed.path == "/api/messages":
            connection = db(); rows = [dict(row) for row in connection.execute("SELECT * FROM messages ORDER BY id DESC LIMIT 100")]; connection.close(); self.send_json(rows); return
        path = "/index.html" if parsed.path in ("", "/") else parsed.path
        file = (ROOT / "static" / path.lstrip("/")).resolve()
        if not str(file).startswith(str((ROOT / "static").resolve())) or not file.is_file():
            self.send_error(404); return
        data = file.read_bytes(); content_type = "text/html" if file.suffix == ".html" else "text/css" if file.suffix == ".css" else "application/javascript"
        self.send_response(200); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = read_body(self)
            if parsed.path == "/webhooks/slack":
                if not signature_ok(env("SLACK_SIGNING_SECRET"), body, self.headers.get("X-Slack-Signature"), "v0="):
                    self.send_json({"error": "invalid signature"}, HTTPStatus.UNAUTHORIZED); return
                payload = json.loads(body or b"{}")
                if payload.get("type") == "url_verification": self.send_json({"challenge": payload.get("challenge", "")}); return
                item = parse_slack_event(payload)
                if item: process_message("slack", *item)
                self.send_json({"ok": True}); return
            if parsed.path == "/webhooks/whatsapp":
                if not signature_ok(env("WHATSAPP_APP_SECRET"), body, self.headers.get("X-Hub-Signature-256")):
                    self.send_json({"error": "invalid signature"}, HTTPStatus.UNAUTHORIZED); return
                for item in parse_whatsapp_messages(json.loads(body or b"{}")): process_message("whatsapp", *item)
                self.send_json({"ok": True}); return
            if parsed.path == "/api/rules":
                payload = json.loads(body or b"{}")
                phrase, reply = str(payload.get("phrase", "")).strip(), str(payload.get("reply", "")).strip()
                channel = payload.get("channel", "both") if payload.get("channel") in ("both", "slack", "whatsapp") else "both"
                if not phrase or not reply: self.send_json({"error": "phrase and reply are required"}, HTTPStatus.BAD_REQUEST); return
                connection = db(); connection.execute("INSERT INTO rules (phrase, reply, channel, created_at) VALUES (?, ?, ?, ?)", (phrase, reply, channel, int(time.time()))); connection.commit(); connection.close(); self.send_json({"ok": True}); return
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            print(f"request failed: {error}", flush=True); self.send_json({"error": "internal error"}, HTTPStatus.INTERNAL_SERVER_ERROR)


if __name__ == "__main__":
    db().close()
    print(f"ReplyDock listening on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
