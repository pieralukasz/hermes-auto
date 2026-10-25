// hermes-hooks: public webhook gateway for Hermes Auto (Gmail Pub/Sub push; Meta later).
// Stores ids and times only, never message content. The Mac connects out to /listen over a
// WebSocket and replays what it missed by sequence number. Secrets are compared as SHA-256 hashes
// (bindings hold only hashes).

const KEEP = 500;

async function sha256(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function matches(secret, expectedHash) {
  if (!secret || !expectedHash) return false;
  const a = new TextEncoder().encode(await sha256(secret));
  const b = new TextEncoder().encode(expectedHash);
  return a.length === b.length && crypto.subtle.timingSafeEqual(a, b);
}

function hub(env) {
  return env.HUB.get(env.HUB.idFromName("hub"));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" && request.method === "GET") return new Response("ok");

    if (url.pathname === "/gmail" && request.method === "POST") {
      if (!(await matches(url.searchParams.get("token"), env.GMAIL_TOKEN_SHA256))) {
        return new Response("forbidden", { status: 403 });
      }
      let historyId = null, messageId = null;
      try {
        const body = await request.json();
        messageId = body?.message?.messageId ?? null;
        const data = JSON.parse(atob(body?.message?.data ?? ""));
        historyId = data.historyId ?? null;
      } catch {
        return new Response("bad request", { status: 400 });
      }
      await hub(env).fetch("https://hub/publish", {
        method: "POST", body: JSON.stringify({ source: "gmail", historyId, messageId }),
      });
      return new Response(null, { status: 204 });
    }

    if (url.pathname === "/todoist" && request.method === "POST") {
      // Todoist signs each delivery: base64 HMAC-SHA256 of the raw body with the app's client secret.
      const body = await request.text();
      if (!env.TODOIST_CLIENT_SECRET) return new Response("not configured", { status: 503 });
      const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(env.TODOIST_CLIENT_SECRET),
        { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
      const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body)));
      const expected = new TextEncoder().encode(btoa(String.fromCharCode(...mac)));
      const given = new TextEncoder().encode(request.headers.get("X-Todoist-Hmac-SHA256") || "");
      if (expected.length !== given.length || !crypto.subtle.timingSafeEqual(expected, given)) {
        return new Response("forbidden", { status: 403 });
      }
      let event = null;
      try { event = JSON.parse(body)?.event_name ?? null; } catch {}
      await hub(env).fetch("https://hub/publish", { method: "POST", body: JSON.stringify({ source: "todoist", event }) });
      return new Response(null, { status: 200 });
    }

    if (url.pathname === "/listen") {
      const auth = request.headers.get("Authorization") || "";
      if (!(await matches(auth.replace(/^Bearer /, ""), env.LISTEN_SHA256))) {
        return new Response("forbidden", { status: 403 });
      }
      if (request.headers.get("Upgrade") !== "websocket") return new Response("upgrade required", { status: 426 });
      return hub(env).fetch(`https://hub/listen?since=${encodeURIComponent(url.searchParams.get("since") || "0")}`,
        { headers: request.headers });
    }
    return new Response("not found", { status: 404 });
  },
};

export class Hub {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  key(seq) {
    return "ev:" + String(seq).padStart(12, "0");
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/publish") {
      const event = await request.json();
      const seq = ((await this.state.storage.get("seq")) || 0) + 1;
      const stored = { ...event, seq, at: Date.now() };
      await this.state.storage.put({ seq, [this.key(seq)]: stored });
      if (seq > KEEP) await this.state.storage.delete(this.key(seq - KEEP));
      for (const socket of this.state.getWebSockets()) {
        try { socket.send(JSON.stringify(stored)); } catch {}
      }
      return new Response("ok");
    }
    if (url.pathname === "/listen") {
      const pair = new WebSocketPair();
      this.state.acceptWebSocket(pair[1]);
      const since = Number(url.searchParams.get("since") || 0);
      const missed = await this.state.storage.list({ prefix: "ev:", start: this.key(since + 1) });
      for (const event of missed.values()) pair[1].send(JSON.stringify(event));
      pair[1].send(JSON.stringify({ type: "hello", seq: (await this.state.storage.get("seq")) || 0 }));
      return new Response(null, { status: 101, webSocket: pair[0] });
    }
    return new Response("not found", { status: 404 });
  }

  webSocketMessage(socket, message) {
    if (message === "ping") socket.send("pong");
  }

  webSocketClose(socket, code) {
    try { socket.close(code, "bye"); } catch {}
  }
}
