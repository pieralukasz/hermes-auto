// Durable Object epoch persists across restarts, but changes with a fresh store.
import assert from "node:assert";
import { readFileSync } from "node:fs";
globalThis.WebSocketPair = class { constructor() { const s = { sent: [], send(m) { this.sent.push(m); } }; this[0] = s; this[1] = s; } };
const OrigResponse = globalThis.Response;
globalThis.Response = class extends OrigResponse { constructor(body, init = {}) { super(body, init.status === 101 ? {} : init); this.webSocket = init.webSocket; } };
const source = readFileSync(new URL("./worker.js", import.meta.url));
const { Hub } = await import(`data:text/javascript;base64,${source.toString("base64")}`);
function storage() {
  const map = new Map();
  return { map,
    async get(k) { return map.get(k); },
    async put(k, v) { if (typeof k === "object") for (const [a, b] of Object.entries(k)) map.set(a, b); else map.set(k, v); },
    async delete(k) { map.delete(k); },
    async list({ prefix, start }) { return new Map([...map].filter(([k]) => k.startsWith(prefix) && k >= start).sort()); } };
}
const sockets = [];
const state = (s) => ({ storage: s, getWebSockets: () => sockets, acceptWebSocket() {} });
async function hello(hub, since = 0) {
  const r = await hub.fetch(new Request(`https://hub/listen?since=${since}`));
  return r.webSocket.sent.map((m) => JSON.parse(m));
}
const s1 = storage();
let hub = new Hub(state(s1), {});
await hub.fetch(new Request("https://hub/publish", { method: "POST", body: JSON.stringify({ source: "gmail" }) }));
const frames = await hello(hub);
const h1 = frames.at(-1);
assert.equal(h1.type, "hello"); assert.equal(h1.seq, 1); assert.ok(h1.epoch, "hello has an epoch");
assert.equal(h1.first, 1);
hub = new Hub(state(s1), {});
assert.equal((await hello(hub, 1)).at(-1).epoch, h1.epoch, "epoch survives a restart");
hub = new Hub(state(storage()), {});
assert.notEqual((await hello(hub)).at(-1).epoch, h1.epoch, "wiped storage gets a new epoch");
console.log("worker ok");
