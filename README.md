# ReplyDock

ReplyDock is a small, self-hosted auto-reply hub for Slack and WhatsApp. It is free software with no paid automation service, queue, database server, or framework required. It uses only Python's standard library (3.9+) and SQLite.

## What it does

- Receives Slack Events API webhooks and replies through `chat.postMessage`.
- Receives WhatsApp Cloud API webhooks and replies through the Graph API.
- Matches simple case-insensitive phrases, or regular expressions prefixed with `re:`.
- Stores rules and recent messages (with the reply and whether it was sent) in a local SQLite file.
- Provides a dashboard at `/` to add, disable, delete, and test rules, see activity, and check which integrations are configured.
- Verifies Slack (`v0` signing secret, 5-minute replay window) and WhatsApp (`X-Hub-Signature-256`) webhook signatures when secrets are configured.
- Acknowledges webhooks immediately and replies in the background, and ignores duplicate deliveries, so Slack/Meta retries never cause double replies.
- Optionally protects the dashboard and API with a password (`REPLYDOCK_ADMIN_TOKEN`).

The software is free and self-hostable. WhatsApp itself still requires Meta Cloud API access and is subject to Meta's account, 24-hour messaging-window, template, and usage policies. Slack requires a Slack app installed in the workspace.

## Run locally

```sh
cp .env.example .env
python3 server.py
```

`server.py` reads `.env` from its own directory automatically (set `REPLYDOCK_ENV_FILE` to use another file). Open `http://127.0.0.1:8080`. The dashboard and the rule tester work without Slack or WhatsApp configured.

## Deploy

Put ReplyDock behind HTTPS (Caddy, nginx, a tunnel, ...) and set:

```sh
REPLYDOCK_HOST=0.0.0.0
REPLYDOCK_ADMIN_TOKEN=a-long-random-password
```

Webhook URLs (these stay public; they are protected by signatures, not the admin password):

- Slack: `https://your-domain.example/webhooks/slack`
- WhatsApp: `https://your-domain.example/webhooks/whatsapp`

Do not expose the server directly over plain HTTP. Keep `.env` and the SQLite file private.

### Docker

```sh
docker build -t replydock .
docker run -d --name replydock -p 8080:8080 --env-file .env -v "$PWD/data:/data" replydock
```

The database lives in the `/data` volume.

## Connect Slack

1. Create an app at <https://api.slack.com/apps> ("From scratch").
2. **OAuth & Permissions** → add bot scopes `chat:write` and `channels:history` (add `groups:history` / `im:history` for private channels or DMs). Install the app and copy the **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN`.
3. **Basic Information** → copy the **Signing Secret** into `SLACK_SIGNING_SECRET`, then restart ReplyDock.
4. **Event Subscriptions** → enable, set the Request URL to `/webhooks/slack` (Slack verifies it immediately), and subscribe to bot events `message.channels` (plus `message.groups` / `message.im` if wanted).
5. Invite the bot to a channel (`/invite @YourApp`) and post a message that matches a rule.

Replies go into a thread under the incoming message; set `SLACK_REPLY_IN_THREAD=0` to reply in the channel instead. Messages from bots (including ReplyDock itself), edits, and joins are ignored.

## Connect WhatsApp

The dashboard has a step-by-step guide at `/setup.html`. It shows which settings are missing, builds your webhook URL, can send a `hello_world` test message, and shows incoming WhatsApp messages as they arrive. Summary:

1. Create a Meta app with the WhatsApp product at <https://developers.facebook.com/apps>.
2. From **WhatsApp → API Setup**, copy the **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID` and an access token (a permanent System User token for production) into `WHATSAPP_ACCESS_TOKEN`.
3. From **App settings → Basic**, copy the **App secret** into `WHATSAPP_APP_SECRET`. Choose any string for `WHATSAPP_VERIFY_TOKEN`. Restart ReplyDock.
4. **WhatsApp → Configuration** → set the callback URL to `/webhooks/whatsapp` and the same verify token, then subscribe to the `messages` field.

Text messages are answered with a free-form text reply, which WhatsApp allows within 24 hours of the customer's last message.

## Rule behavior

A phrase is matched case-insensitively anywhere in an incoming message. A phrase beginning with `re:` is a case-insensitive regular expression (for example `re:^order\s+\d+$`). Rules target `both`, `slack`, or `whatsapp`; the first matching enabled rule (lowest id) wins. Use the dashboard's tester to see which rule a message would trigger.

## API

All `/api/*` routes require the admin password when `REPLYDOCK_ADMIN_TOKEN` is set (HTTP Basic auth, any username).

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/rules` | List rules |
| POST | `/api/rules` | Add a rule: `{"phrase", "reply", "channel"}` |
| POST | `/api/rules/<id>/toggle` | Enable/disable a rule |
| DELETE | `/api/rules/<id>` | Delete a rule |
| POST | `/api/test` | Preview: `{"text", "channel"}` → matching reply, nothing is sent |
| POST | `/api/whatsapp/test` | Send the `hello_world` template: `{"to": "+60123456789"}` |
| GET | `/api/messages` | Last 100 messages with reply and send status |
| GET | `/api/status` | Which integration settings are present |
| GET | `/healthz` | Liveness check (no auth) |

## Tests

```sh
python3 -m unittest discover -s tests
```

## License

MIT.
