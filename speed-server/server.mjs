/**
 * isitfast India speed server — speed-in.isitfast.cloud (GCE asia-south1, behind Caddy for TLS)
 *
 * Same protocol as speed-worker/src/index.js (the Cloudflare Worker), so the page and the origin's
 * receipt check treat both alike:
 *   GET  /__down?bytes=N[&s=sid&b=block]  N bytes; with s=, the last 128 bytes of every block are a
 *                                          signed cumulative receipt "sid|d|bytes|t0|t1|rid|sig"
 *   POST /__up[?s=sid]                     counts the body; replies "sid|u|bytes|t0|t1|rid|sig"
 *   GET  /cdn-cgi/trace                    "colo=GCPBOM" so saved tests show which server ran them
 *   GET  /healthz
 * sig = first 32 hex of HMAC-SHA256(RECEIPT_KEY, "sid|dir|bytes|t0|t1|rid"). RECEIPT_KEY comes from
 * /etc/isitfast-speed.env (same secret as the Worker and the origin). Zero dependencies (Node >= 18).
 */
import http from "node:http";
import crypto from "node:crypto";

const PORT = Number(process.env.PORT || 8080);
const KEY = process.env.RECEIPT_KEY || "";
const COLO = process.env.COLO || "GCPBOM";
const BLOCK = 4 * 1024 * 1024;
const RECEIPT = 128;
const MAX_DOWN = 500e6;
const MAX_UP = 200e6;
const SID = /^[0-9a-f]{16}$/;
const ALLOWED = ["https://isitfast.cloud", "https://www.isitfast.cloud", "https://isitfast.space",
  "https://www.isitfast.space", "http://127.0.0.1:8766", "http://localhost:8766"];

const filler = crypto.randomBytes(BLOCK); // incompressible, reused (never copied)
const rid = () => crypto.randomBytes(4).toString("hex");

function receipt(sid, dir, bytes, t0, t1, id) {
  const msg = `${sid}|${dir}|${bytes}|${t0}|${t1}|${id}`;
  return `${msg}|${crypto.createHmac("sha256", KEY).update(msg).digest("hex").slice(0, 32)}`;
}

function cors(req) {
  const o = req.headers.origin;
  return {
    "access-control-allow-origin": ALLOWED.includes(o) ? o : ALLOWED[0],
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type",
    "access-control-max-age": "86400",
    "timing-allow-origin": "*",
    vary: "origin",
  };
}

const write = (res, buf) => new Promise((ok) => (res.write(buf) ? ok() : res.once("drain", ok)));

async function down(req, res, url) {
  const n = Math.max(0, Math.min(Number(url.searchParams.get("bytes")) || 0, MAX_DOWN));
  const bq = Number(url.searchParams.get("b"));
  const block = bq >= 262144 && bq <= BLOCK && (bq & (bq - 1)) === 0 ? bq : BLOCK;
  const sid = url.searchParams.get("s");
  const headers = { ...cors(req), "content-type": "application/octet-stream", "cache-control": "no-store, no-transform" };
  if (n === 0) { res.writeHead(200, { ...headers, "content-length": "0" }); return res.end(); }
  res.writeHead(200, { ...headers, "content-length": String(n) });
  const signed = sid && SID.test(sid) && KEY;
  const id = rid();
  const t0 = Date.now();
  let sent = 0, gone = false;
  res.on("close", () => { gone = true; });
  while (sent < n && !gone) {
    const size = Math.min(block, n - sent);
    if (!signed || size <= RECEIPT) {
      await write(res, filler.subarray(0, size));
      sent += size;
      continue;
    }
    await write(res, filler.subarray(0, size - RECEIPT));
    sent += size;
    await write(res, Buffer.from(receipt(sid, "d", sent, t0, Date.now(), id).padEnd(RECEIPT - 1, " ") + "\n"));
  }
  res.end();
}

function up(req, res, url) {
  const sid = url.searchParams.get("s");
  let bytes = 0, t0 = 0;
  req.on("data", (c) => {
    if (!t0) t0 = Date.now();
    bytes += c.length;
    if (bytes > MAX_UP) req.destroy();
  });
  req.on("end", () => {
    const t1 = Date.now();
    const body = sid && SID.test(sid) && KEY ? receipt(sid, "u", bytes, t0 || t1, t1, rid()) : "";
    res.writeHead(200, { ...cors(req), "content-type": "text/plain", "cache-control": "no-store" });
    res.end(body);
  });
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://x");
  if (req.method === "OPTIONS") { res.writeHead(204, cors(req)); return res.end(); }
  if (url.pathname === "/__down" && req.method === "GET") return void down(req, res, url).catch(() => res.destroy());
  if (url.pathname === "/__up" && req.method === "POST") return up(req, res, url);
  if (url.pathname === "/cdn-cgi/trace") {
    res.writeHead(200, { ...cors(req), "content-type": "text/plain", "cache-control": "no-store" });
    return res.end(`colo=${COLO}\nloc=IN\n`);
  }
  if (url.pathname === "/healthz") { res.writeHead(200, cors(req)); return res.end("ok"); }
  res.writeHead(404, cors(req));
  res.end("not found");
});
server.keepAliveTimeout = 65_000;
server.requestTimeout = 0; // long uploads/downloads
server.listen(PORT, "127.0.0.1", () => console.log(`speed server on 127.0.0.1:${PORT}, receipts ${KEY ? "on" : "OFF"}`));
