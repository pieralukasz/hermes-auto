# hermes-hooks gateway

Cloudflare Worker + Durable Object at `https://hooks.lucaspiera.com` (account of the repo owner). Receives webhooks that need a public address and forwards ids-only events to the Mac over an outbound WebSocket (`hermes-auto-hooks-listen`).

- `POST /gmail?token=…`: Gmail Pub/Sub push (`projects/gen-lang-client-0274098429/subscriptions/hermes-gmail-push` -> topic `hermes-gmail`). Stores `historyId`/`messageId` only.
- `GET /listen?since=N` (WebSocket, `Authorization: Bearer …`): replays events after sequence N, then streams.
- Secrets live in the Mac keychain (`hermes-auto-hooks-listen`, `hermes-auto-hooks-gmail`); `wrangler.toml` holds only their SHA-256.

Deploy: `npx wrangler deploy` (logged in with `wrangler login`).
