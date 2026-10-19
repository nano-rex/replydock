# ReplyDock

ReplyDock is a small, self-hosted auto-reply hub for Slack and WhatsApp. It is free software with no paid automation service, queue, database, or framework required. It uses Python's standard library and SQLite.

## What it does

- Receives Slack Events API webhooks and replies through `chat.postMessage`.
- Receives WhatsApp Cloud API webhooks and replies through the Graph API.
- Matches simple case-insensitive phrases, or regex rules prefixed with `re:`.
- Stores rules and recent messages in a local SQLite file.
- Provides a local dashboard at `/` for adding rules and viewing activity.
- Verifies Slack and WhatsApp webhook signatures when secrets are configured.

The software is free and self-hostable. WhatsApp itself still requires Meta Business/Cloud API access and is subject to Meta's account, messaging-window, template, and possible usage policies. Slack requires a Slack app in the workspace.

## Run locally

```sh
cp .env.example .env
python3 server.py
```

Open `http://127.0.0.1:8080`. The dashboard works locally without Slack or WhatsApp configured.

For a server, put it behind HTTPS and a reverse proxy, then set `REPLYDOCK_HOST=0.0.0.0`. Webhook URLs are:

- Slack: `https://your-domain.example/webhooks/slack`
- WhatsApp: `https://your-domain.example/webhooks/whatsapp`

Do not expose the server directly over HTTP to the public internet. Keep `.env` and `replydock.sqlite3` private.

## Docker

```sh
docker build -t replydock .
docker run --rm -p 8080:8080 --env-file .env -v "$PWD/data:/app" replydock
```

## Rule behavior

A phrase is matched anywhere in an incoming message. A phrase beginning with `re:` is treated as a regular expression. Rules can target `both`, `slack`, or `whatsapp`; the first matching enabled rule wins.

## License

MIT.
