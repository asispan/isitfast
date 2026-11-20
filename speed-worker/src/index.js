/**
 * isitfast speed endpoint — speed.isitfast.cloud
 *
 * Same shape as speed.cloudflare.com (GET /__down?bytes=N, POST /__up, /cdn-cgi/trace works on the
 * zone), plus signed byte receipts so the origin can check a submitted result against what this
 * Worker actually served/received:
 *
 *  - Download bodies are split into blocks (?b=, default 4 MiB; the site asks for 1 MiB); the last 128
 *    bytes of every block are an ASCII receipt "sid|d|bytes|t0|t1|rid|sig" (cumulative bytes for this
 *    response, Worker clock ms, per-response id), so a
 *    client that is cut off mid-stream still holds a receipt for everything it received so far.
 *  - Uploads are counted as they arrive; the response body is the receipt "sid|u|bytes|t0|t1|sig".
 *
 * sig = first 32 hex chars of HMAC-SHA256(RECEIPT_KEY, "sid|dir|bytes|t0|t1"). The origin holds the
 * same key. sid is the single-use test nonce issued by the origin's test-start, passed as ?s=.
 *
 * Responses are application/octet-stream + no-transform, so nothing on the path compresses them.
 */
const BLOCK = 4 * 1024 * 1024;
const RECEIPT = 128;
const MAX_DOWN = 500e6;
const ALLOWED = ["https://isitfast.cloud", "https://www.isitfast.cloud", "https://isitfast.space", "https://www.isitfast.space", "http://127.0.0.1:8766", "http://localhost:8766"];

let filler = null; // one random block per isolate: incompressible, reused (never copied)
let keyPromise = null;

function cors(req) {
  const o = req.headers.get("origin");
  return {
    "access-control-allow-origin": ALLOWED.includes(o) ? o : ALLOWED[0],
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-max-age": "86400",
    "timing-allow-origin": "*",
    vary: "origin",
  };
}

function hmacKey(env) {
  if (!keyPromise) {
    keyPromise = crypto.subtle.importKey("raw", new TextEncoder().encode(env.RECEIPT_KEY), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  }
  return keyPromise;
}

// rid: random per response. Workers' Date.now() only advances on I/O, so parallel responses often share
// t0; the origin groups a response's cumulative receipts by rid.
const rid = () => [...crypto.getRandomValues(new Uint8Array(4))].map((b) => b.toString(16).padStart(2, "0")).join("");

async function receipt(env, sid, dir, bytes, t0, t1, id) {
  const msg = `${sid}|${dir}|${bytes}|${t0}|${t1}|${id}`;
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", await hmacKey(env), new TextEncoder().encode(msg)));
  return `${msg}|${[...sig.slice(0, 16)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

const SID = /^[0-9a-f]{16}$/;

async function down(req, env, url) {
  const n = Math.max(0, Math.min(Number(url.searchParams.get("bytes")) || 0, MAX_DOWN));
  // receipt spacing chosen by the client (power of two, 256 KiB..4 MiB); default 4 MiB for old pages
  const bq = Number(url.searchParams.get("b"));
  const block = bq >= 262144 && bq <= BLOCK && (bq & (bq - 1)) === 0 ? bq : BLOCK;
  const sid = url.searchParams.get("s");
  const headers = { ...cors(req), "content-type": "application/octet-stream", "cache-control": "no-store, no-transform" };
  if (n === 0) return new Response(null, { headers }); // latency probe
  if (!filler) {
    filler = new Uint8Array(BLOCK);
    for (let i = 0; i < BLOCK; i += 65536) crypto.getRandomValues(filler.subarray(i, i + 65536));
  }
  const signed = sid && SID.test(sid) && env.RECEIPT_KEY;
  const id = rid();
  let sent = 0;
  let t0 = 0;
  const enc = new TextEncoder();
  const stream = new ReadableStream({
    async pull(ctrl) {
      if (!t0) t0 = Date.now();
      const left = n - sent;
      if (left <= 0) return ctrl.close();
      const size = Math.min(block, left);
      if (!signed || size <= RECEIPT) {
        ctrl.enqueue(filler.subarray(0, size));
        sent += size;
        return;
      }
      ctrl.enqueue(filler.subarray(0, size - RECEIPT)); // filler is BLOCK long, so any block <= BLOCK fits
      sent += size;
      const r = (await receipt(env, sid, "d", sent, t0, Date.now(), id)).padEnd(RECEIPT - 1, " ") + "\n";
      ctrl.enqueue(enc.encode(r));
    },
  }, { highWaterMark: 0 });
  return new Response(stream, { headers: { ...headers, "content-length": String(n) } });
}

async function up(req, env, url) {
  const sid = url.searchParams.get("s");
  const headers = { ...cors(req), "content-type": "text/plain", "cache-control": "no-store" };
  let bytes = 0;
  let t0 = 0;
  if (req.body) {
    const rd = req.body.getReader();
    for (;;) {
      const { done, value } = await rd.read();
      if (done) break;
      if (!t0) t0 = Date.now();
      bytes += value.byteLength;
    }
  }
  const t1 = Date.now();
  if (!sid || !SID.test(sid) || !env.RECEIPT_KEY) return new Response("", { headers });
  return new Response(await receipt(env, sid, "u", bytes, t0 || t1, t1, rid()), { headers });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { headers: cors(req) });
    if (url.pathname === "/__down" && req.method === "GET") return down(req, env, url);
    if (url.pathname === "/__up" && req.method === "POST") return up(req, env, url);
    if (url.pathname === "/healthz") return new Response("ok", { headers: cors(req) });
    return new Response("not found", { status: 404, headers: cors(req) });
  },
};
