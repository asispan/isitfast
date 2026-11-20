/* In-browser speed test against our own Cloudflare Worker (speed.isitfast.cloud, nearest edge) or our
   India server (speed-in.isitfast.cloud, GCE Mumbai; speed-server/) when it is clearly closer, with
   Cloudflare's public speed.cloudflare.com as a fallback. All expose /__down and /__up.

   Our Worker and India server sign byte receipts (speed-worker/src/index.js, speed-server/server.mjs): every 4 MiB of a download ends in a
   128-byte receipt, and each upload response is one. They are sent with the result so the server can
   check the claimed speeds against data that was really transferred.

   Methodology (close to Ookla/fast.com, so numbers are comparable):
   - Latency: idle round trips on a warm connection, timed with Resource Timing
     (requestStart→responseStart) minus the server's own processing time.
   - Throughput: several parallel connections, run for a fixed time window; bytes
     are counted as they arrive (download) / leave (upload), sampled every 250 ms.
     The TCP slow-start warm-up is discarded, then the slowest 30% and fastest 10%
     of the remaining samples are trimmed and the rest averaged.
     A single sequential connection — what this file used to do — badly
     under-reads high-latency or lossy links (mobile data, hotspots).

   Returns { down_mbps, up_mbps, ping_ms, jitter_ms, asn, colo }. */
(function () {
  const OWN = "https://speed.isitfast.cloud", INDIA = "https://speed-in.isitfast.cloud";
  const FALLBACK = "https://speed.cloudflare.com";
  // QA override, read once at load: the app rewrites the address to /p/<id> (dropping the query) before
  // a test starts, so it is kept for the tab in sessionStorage. ?speed=auto clears it.
  const FORCE = (() => {
    try {
      const q = new URLSearchParams(location.search).get("speed");
      if (q === "india" || q === "own") sessionStorage.setItem("isitfast.speed", q);
      else if (q === "auto") sessionStorage.removeItem("isitfast.speed");
      return sessionStorage.getItem("isitfast.speed");
    } catch { return null; }
  })();
  // Receipt every 1 MiB: on mobile data a stream may only run ~5 s, so coarse receipts undercount it.
  const BLOCK = 1024 * 1024, RECEIPT = 128;
  let BASE = OWN, SID = null, moved = 0, ownFailed = false;
  let receipts = [];
  // Fixed broadband: long windows and big caps so gigabit links are measured properly.
  // Mobile data / Data Saver: shorter windows and hard caps (~75 MB down + ~25 MB up at most).
  const PROFILES = {
    full: { DOWN: { streams: 6, chunk: 50e6, minMs: 6000, maxMs: 12000, capBytes: 400e6 },
            UP: { streams: 4, chunk: 2e6, minMs: 6000, maxMs: 10000, capBytes: 150e6 } },
    frugal: { DOWN: { streams: 4, chunk: 25e6, minMs: 5000, maxMs: 9000, capBytes: 75e6 },
              // 0.5 MB upload chunks: each completed chunk returns a receipt, and a 2–3 Mbps mobile
              // uplink must finish several within the 5–8 s window
              UP: { streams: 3, chunk: 0.5e6, minMs: 5000, maxMs: 8000, capBytes: 25e6 } },
  };
  let DOWN = PROFILES.full.DOWN, UP = PROFILES.full.UP;
  const TICK_MS = 250, WARMUP_MS = 1500, WINDOW_MS = 1000;

  const now = () => performance.now();
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rnd = () => Math.random().toString(36).slice(2);
  const median = (a) => { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

  // Mean of samples after dropping the slowest 30% and fastest 10%.
  function trimmedMean(a) {
    const s = [...a].sort((x, y) => x - y);
    const lo = Math.floor(s.length * 0.3), hi = Math.max(lo + 1, Math.ceil(s.length * 0.9));
    const k = s.slice(lo, hi);
    return k.length ? k.reduce((x, y) => x + y, 0) / k.length : 0;
  }

  function serverMs(res) {
    // e.g. "cfSpeedEdge;dur=4, cfSpeedWorker;dur=20" — both are time spent inside Cloudflare, not on the wire.
    return [...(res.headers.get("server-timing") || "").matchAll(/dur=([\d.]+)/g)].reduce((s, m) => s + parseFloat(m[1]), 0);
  }

  async function latency(n, onTick) {
    const samples = [];
    for (let i = 0; i <= n; i++) {
      const url = `${BASE}/__down?bytes=0&r=${rnd()}`;
      const t0 = now();
      const res = await fetch(url, { cache: "no-store" });
      await res.arrayBuffer();
      const wall = now() - t0;
      const e = performance.getEntriesByName(url).pop();
      const net = e && e.responseStart > 0 && e.requestStart > 0 ? e.responseStart - e.requestStart : wall;
      if (i === 0) continue; // first request may pay DNS/TCP/TLS setup
      samples.push(Math.max(1, net - serverMs(res)));
      onTick && onTick(median(samples)); // same statistic as the saved result
    }
    performance.clearResourceTimings();
    const jit = samples.slice(1).map((v, i) => Math.abs(v - samples[i]));
    return { ping: median(samples), jitter: jit.length ? jit.reduce((a, b) => a + b, 0) / jit.length : 0 };
  }

  async function colo() {
    try {
      const t = await (await fetch(`${BASE}/cdn-cgi/trace`, { cache: "no-store" })).text();
      return (/^colo=(\w+)/m.exec(t) || [])[1] || null;
    } catch { return null; }
  }

  // Drive `startStream` on cfg.streams parallel connections; it must call add(bytes)
  // as data moves and resolve when its transfer ends. Returns Mbps.
  async function measure(cfg, startStream, onTick) {
    let bytes = 0, stop = false, errors = 0;
    const add = (b) => { bytes += b; moved += b; };
    const aborters = new Set();
    const loop = async () => {
      while (!stop) {
        try { await startStream(add, aborters); }
        catch (e) { if (stop) return; if (++errors > 10) throw e; await sleep(200); }
      }
    };
    const t0 = now();
    const workers = Array.from({ length: cfg.streams }, loop);
    const hist = [[0, 0]]; // [t, cumulative bytes]
    const rates = [];
    let failure = null;
    Promise.all(workers).catch((e) => { failure = e; });
    while (!failure) {
      await sleep(TICK_MS);
      const t = now() - t0;
      hist.push([t, bytes]);
      // rolling 1 s rate smooths out bursty chunk delivery
      let j = hist.length - 1;
      while (j > 0 && t - hist[j - 1][0] <= WINDOW_MS) j--;
      const [tb, bb] = hist[Math.max(0, j - 1)];
      const r = t > tb ? ((bytes - bb) * 8) / ((t - tb) / 1000) / 1e6 : 0;
      onTick && onTick(r);
      if (t >= WARMUP_MS) rates.push(r);
      if (t >= cfg.maxMs || bytes >= cfg.capBytes) break;
      if (t >= cfg.minMs && rates.length >= 12) {
        // stop early once the last two seconds agree within 5%
        const a = rates.slice(-4), b = rates.slice(-8, -4);
        const ma = a.reduce((x, y) => x + y) / 4, mb = b.reduce((x, y) => x + y) / 4;
        if (mb > 0 && Math.abs(ma - mb) / mb < 0.05) break;
      }
    }
    stop = true;
    aborters.forEach((ab) => ab());
    if (failure && !rates.length) throw failure;
    if (!rates.length) { // tiny transfer budget hit before warm-up ended: fall back to overall average
      const t = (now() - t0) / 1000;
      return t > 0 ? (bytes * 8) / t / 1e6 : 0;
    }
    return trimmedMean(rates);
  }

  function downStream(add, aborters) {
    const ctrl = new AbortController();
    const ab = () => ctrl.abort();
    aborters.add(ab);
    let last = null;
    return (async () => {
      try {
        const n = DOWN.chunk;
        const res = await fetch(`${BASE}/__down?bytes=${n}${SID ? `&s=${SID}&b=${BLOCK}` : ""}&r=${rnd()}`, { cache: "no-store", signal: ctrl.signal });
        const rd = res.body.getReader();
        // receipt windows: the last RECEIPT bytes before every BLOCK boundary (and before the end)
        let off = 0, k = 1, buf = [];
        last = null;
        const winEnd = () => Math.min(k * BLOCK, n);
        for (;;) {
          const { done, value } = await rd.read();
          if (done) break;
          add(value.byteLength);
          if (SID) {
            let from = off;
            const to = off + value.byteLength;
            while (from < to) {
              const e = winEnd(), b = e - RECEIPT;
              if (to <= b) break;
              const s0 = Math.max(from, b), s1 = Math.min(to, e);
              if (s1 > s0) buf.push(value.subarray(s0 - off, s1 - off));
              if (s1 === e) {
                const r = new TextDecoder().decode(concat(buf)).trim();
                if (r.startsWith(SID)) last = r;
                buf = []; k++;
                if (e >= n) break;
              }
              from = s1;
            }
          }
          off += value.byteLength;
        }
      } finally {
        aborters.delete(ab);
        if (last) receipts.push(last); // streams are aborted when the test stops: keep what was proven
      }
    })();
  }

  // a stream cut off mid-response still keeps the last complete receipt it saw
  function concat(parts) {
    const out = new Uint8Array(parts.reduce((a, p) => a + p.length, 0));
    let o = 0;
    for (const p of parts) { out.set(p, o); o += p.length; }
    return out;
  }

  let upBlob = null;
  function payload() {
    if (upBlob) return upBlob;
    const block = new Uint8Array(65536);
    crypto.getRandomValues(block); // incompressible, so no middlebox can shrink it
    upBlob = new Blob(Array.from({ length: Math.ceil(UP.chunk / block.length) }, () => block));
    return upBlob;
  }

  function upStream(add, aborters) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      const body = payload();
      let sent = 0;
      const ab = () => x.abort();
      aborters.add(ab);
      x.upload.onprogress = (e) => { add(e.loaded - sent); sent = e.loaded; };
      x.upload.onload = () => { add(body.size - sent); sent = body.size; };
      x.onloadend = () => {
        aborters.delete(ab);
        if (SID && x.status === 200 && x.responseText.startsWith(SID)) receipts.push(x.responseText.trim());
        x.status || sent ? resolve() : reject(new Error("upload failed"));
      };
      x.open("POST", `${BASE}/__up?${SID ? `s=${SID}&` : ""}r=${rnd()}`);
      x.send(body);
    });
  }

  // Our Cloudflare Worker or our India server (GCE Mumbai), whichever is clearly closer, else Cloudflare's
  // public endpoint (no receipts then). Several Indian ISPs (Hathway, often Airtel/Jio) send traffic for
  // Cloudflare free-plan sites to Singapore or Marseille, 60–160 ms away, which reads far below the line's
  // real speed; the India server is ~10–40 ms from most Indian users. Cloudflare stays the default when
  // it is about as close, since its traffic costs us nothing.
  async function rtt(base) {
    const once = async () => {
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), 3000);
      const t0 = performance.now();
      try {
        const r = await fetch(`${base}/__down?bytes=0&r=${rnd()}`, { cache: "no-store", signal: ctrl.signal });
        return r.ok ? performance.now() - t0 : Infinity;
      } catch { return Infinity; } finally { clearTimeout(t); }
    };
    if (await once() === Infinity) return Infinity; // the first round trip also opens the connection
    let best = Infinity;
    for (let i = 0; i < 3; i++) best = Math.min(best, await once());
    return best;
  }

  async function pickEndpoint(sid) {
    const force = FORCE; // QA: ?speed=india|own
    if (force === "india" || force === "own") { BASE = force === "india" ? INDIA : OWN; SID = sid || null; return; }
    const [own, india] = await Promise.all([rtt(OWN), rtt(INDIA)]);
    ownFailed = own === Infinity;
    const useIndia = india !== Infinity && (own === Infinity || india < own - 15 || india < own * 0.6);
    if (useIndia || own !== Infinity) {
      BASE = useIndia ? INDIA : OWN; SID = sid || null;
      return;
    }
    BASE = FALLBACK; SID = null;
  }

  async function run({ network, sid, onPing, onDown, onUp, onPhase } = {}) {
    const frugal = network === "cellular" || !!(navigator.connection && navigator.connection.saveData);
    ({ DOWN, UP } = frugal ? PROFILES.frugal : PROFILES.full);
    upBlob = null;
    receipts = [];
    onPhase && onPhase("Connecting…");
    await pickEndpoint(sid);
    const [lat, where] = await Promise.all([latency(15, onPing), colo()]);
    onPhase && onPhase("Measuring download…");
    moved = 0;
    const down = await measure(DOWN, downStream, onDown);
    const downMB = moved / 1e6;
    onPhase && onPhase("Measuring upload…");
    const up = await measure(UP, upStream, onUp);
    // ASN is taken server-side from Cloudflare's view of the submit request.
    return { down_mbps: down, up_mbps: up, ping_ms: lat.ping, jitter_ms: lat.jitter, asn: null, colo: where,
      receipts: SID ? receipts.slice(0, 400) : null,
      meta: { endpoint: BASE === OWN ? "own" : BASE === INDIA ? "india" : "fallback", own_failed: ownFailed,
              down_mb: Math.round(downMB * 10) / 10, up_mb: Math.round((moved / 1e6 - downMB) * 10) / 10 } };
  }

  window.SpeedTest = { run };
})();
