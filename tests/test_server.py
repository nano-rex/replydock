import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["REPLYDOCK_ENV_FILE"] = os.devnull
import server  # noqa: E402

ENV_KEYS = ("SLACK_SIGNING_SECRET", "SLACK_BOT_TOKEN", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "REPLYDOCK_ADMIN_TOKEN")


class ReplyDockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {key: "" for key in ENV_KEYS} | {"REPLYDOCK_DB": os.path.join(self.tmp.name, "test.sqlite3")})
        self.env.start()
        server.init_db()
        self.sent = []
        self.remote = mock.patch.object(server, "request_json", side_effect=lambda url, payload, headers=None: self.sent.append((url, payload)) or {"ok": True})
        self.remote.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.remote.stop()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, path, payload=None, method=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data is not None else "GET"), headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def wait_for_messages(self, count):
        for _ in range(100):
            rows = json.loads(self.call("/api/messages")[1])
            if len(rows) >= count:
                return rows
            time.sleep(0.02)
        self.fail("messages were not processed")

    def test_rule_matching_strips_regex_prefix(self):
        self.assertTrue(server.rule_matches("re:^order\\s+\\d+$", "Order 42"))
        self.assertFalse(server.rule_matches("re:^order\\s+\\d+$", "my order 42"))
        self.assertTrue(server.rule_matches("Hello", "well hello there"))
        self.assertFalse(server.rule_matches("re:(", "("))

    def test_rule_crud_and_preview(self):
        status, body = self.call("/api/rules", {"phrase": "re:price|cost", "reply": "See pricing", "channel": "whatsapp"})
        self.assertEqual(status, 200)
        rule_id = json.loads(body)["id"]
        self.assertEqual(self.call("/api/rules", {"phrase": "re:(", "reply": "x"})[0], 400)
        result = json.loads(self.call("/api/test", {"text": "what does it cost", "channel": "whatsapp"})[1])
        self.assertEqual(result["reply"], "See pricing")
        self.assertFalse(json.loads(self.call("/api/test", {"text": "what does it cost", "channel": "slack"})[1])["matched"])
        self.assertEqual(self.call(f"/api/rules/{rule_id}/toggle", {})[0], 200)
        self.assertFalse(json.loads(self.call("/api/test", {"text": "cost", "channel": "whatsapp"})[1])["matched"])
        self.assertEqual(self.call(f"/api/rules/{rule_id}", method="DELETE")[0], 200)
        self.assertEqual(self.call(f"/api/rules/{rule_id}", method="DELETE")[0], 404)

    def test_slack_signature_and_reply(self):
        os.environ["SLACK_SIGNING_SECRET"] = "shh"
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
        event = {"type": "event_callback", "event_id": "Ev1", "event": {"type": "message", "user": "U1", "text": "hello bot", "channel": "C1", "ts": "1.2"}}
        body = json.dumps(event).encode()
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(b"shh", f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
        self.assertEqual(self.call("/webhooks/slack", raw=body, headers={"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": "v0=bad"})[0], 401)
        headers = {"X-Slack-Request-Timestamp": timestamp, "X-Slack-Signature": signature}
        self.assertEqual(self.call("/webhooks/slack", raw=body, headers=headers)[0], 200)
        self.assertEqual(self.call("/webhooks/slack", raw=body, headers=headers)[0], 200)  # Slack retry
        rows = self.wait_for_messages(1)
        time.sleep(0.1)
        self.assertEqual(len(json.loads(self.call("/api/messages")[1])), 1)
        self.assertEqual(rows[0]["status"], "sent")
        self.assertEqual(self.sent, [("https://slack.com/api/chat.postMessage", {"channel": "C1", "text": "Hello! This is an automatic reply.", "thread_ts": "1.2"})])

    def test_slack_url_verification_and_stale_signature(self):
        os.environ["SLACK_SIGNING_SECRET"] = "shh"
        body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
        old = str(int(time.time()) - 600)
        signature = "v0=" + hmac.new(b"shh", f"v0:{old}:".encode() + body, hashlib.sha256).hexdigest()
        self.assertEqual(self.call("/webhooks/slack", raw=body, headers={"X-Slack-Request-Timestamp": old, "X-Slack-Signature": signature})[0], 401)
        now = str(int(time.time()))
        signature = "v0=" + hmac.new(b"shh", f"v0:{now}:".encode() + body, hashlib.sha256).hexdigest()
        status, response = self.call("/webhooks/slack", raw=body, headers={"X-Slack-Request-Timestamp": now, "X-Slack-Signature": signature})
        self.assertEqual((status, json.loads(response)), (200, {"challenge": "abc"}))

    def test_whatsapp_verify_and_reply(self):
        os.environ.update(WHATSAPP_VERIFY_TOKEN="vt", WHATSAPP_APP_SECRET="app", WHATSAPP_ACCESS_TOKEN="EA", WHATSAPP_PHONE_NUMBER_ID="123")
        self.assertEqual(self.call("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42"), (200, b"42"))
        self.assertEqual(self.call("/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=no&hub.challenge=42")[0], 403)
        payload = {"entry": [{"changes": [{"value": {"messages": [{"id": "wamid.1", "from": "60123", "type": "text", "text": {"body": "I need help"}}]}}]}]}
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"app", body, hashlib.sha256).hexdigest()
        self.assertEqual(self.call("/webhooks/whatsapp", raw=body)[0], 401)
        self.assertEqual(self.call("/webhooks/whatsapp", raw=body, headers={"X-Hub-Signature-256": signature})[0], 200)
        self.wait_for_messages(1)
        url, sent = self.sent[0]
        self.assertTrue(url.endswith("/123/messages"))
        self.assertEqual(sent["to"], "60123")
        self.assertIn("follow up", sent["text"]["body"])

    def test_whatsapp_test_template(self):
        self.assertEqual(self.call("/api/whatsapp/test", {"to": "+60 12-345 6789"})[0], 502)
        os.environ.update(WHATSAPP_ACCESS_TOKEN="EA", WHATSAPP_PHONE_NUMBER_ID="123")
        self.assertEqual(self.call("/api/whatsapp/test", {"to": "12"})[0], 400)
        self.assertEqual(self.call("/api/whatsapp/test", {"to": "+60 12-345 6789"})[0], 200)
        self.assertEqual(self.sent[0][1]["to"], "60123456789")
        self.assertEqual(self.sent[0][1]["template"]["name"], "hello_world")

    def test_admin_token_protects_dashboard_not_webhooks(self):
        os.environ["REPLYDOCK_ADMIN_TOKEN"] = "pw"
        self.assertEqual(self.call("/api/rules")[0], 401)
        self.assertEqual(self.call("/")[0], 401)
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
        self.assertEqual(self.call("/api/rules", headers=auth)[0], 200)
        self.assertEqual(self.call("/healthz")[0], 200)
        self.assertEqual(self.call("/webhooks/slack", {"type": "url_verification", "challenge": "c"})[0], 200)

    def test_failed_reply_is_recorded(self):
        server.process_message("slack", "U1", "hello", {"channel": "C1"})
        rows = json.loads(self.call("/api/messages")[1])
        self.assertTrue(rows[0]["status"].startswith("failed: SLACK_BOT_TOKEN"))

    def test_static_path_traversal_blocked(self):
        self.assertEqual(self.call("/../server.py")[0], 404)
        self.assertEqual(self.call("/app.js")[0], 200)
        self.assertEqual(self.call("/setup.html")[0], 200)


if __name__ == "__main__":
    unittest.main()
