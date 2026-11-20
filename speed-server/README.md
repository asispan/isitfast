# India speed server — `speed-in.isitfast.cloud`

A second speed-test endpoint that runs in India, on a Google Cloud VM in Mumbai (`asia-south1-a`). It works exactly like the Cloudflare Worker in `../speed-worker` (`/__down`, `/__up`, the same signed byte receipts and the same `RECEIPT_KEY`), so the origin checks results from both in the same way.

**Why it exists:** our Worker runs on a Cloudflare free-plan zone, and several Indian ISPs send free-plan traffic to Cloudflare in Singapore or Marseille instead of India. Measured in our own test data, Hathway users always landed in SIN or MRS (52–160 ms) and read 55–198 Mbps down / 4–31 Mbps up on a line that measures 221/214 Mbps on Speedtest.net. Airtel and Jio also often land abroad. `web/speedtest.js` measures the round-trip time to both servers and uses this one only when it is clearly closer (more than 15 ms faster, or under 60% of the Worker's time), because Cloudflare's traffic is free and this server pays per GB of outgoing data. For QA, `?speed=india` or `?speed=own` forces a choice for the rest of the browser tab (kept in sessionStorage, because the app drops the query string when it opens a place). `?speed=auto` clears it.

| Part | What |
|---|---|
| VM | `speed-in-1`, e2-medium (2 shared vCPU, 4 GB), Debian 12, Premium network tier, project `is-it-fast-asis` |
| IP | static `34.93.251.178` (`speed-in`), DNS-only A record (not proxied — proxying would route it back through Cloudflare) |
| TLS | Caddy, automatic Let's Encrypt, HTTP/1.1 + HTTP/2 → `127.0.0.1:8080` |
| App | `server.mjs`, Node 18, no dependencies, systemd `isitfast-speed` |
| Secret | `/etc/isitfast-speed.env` (`RECEIPT_KEY=…`, mode 600), copied from the origin; never committed |
| Kernel | BBR; send buffer capped at 8 MB so download receipts can't run far ahead of delivery (`startup.sh`) |

## Deploy an update

```bash
gcloud compute scp server.mjs Caddyfile isitfast-speed.service speed-in-1:/tmp/ --zone=asia-south1-a
gcloud compute ssh speed-in-1 --zone=asia-south1-a --command='sudo install -m644 /tmp/server.mjs /opt/isitfast-speed/ && sudo install -m644 /tmp/Caddyfile /etc/caddy/Caddyfile && sudo systemctl restart isitfast-speed && sudo systemctl reload caddy'
curl -s https://speed-in.isitfast.cloud/cdn-cgi/trace   # colo=GCPBOM
```

## Cost

The money goes on outgoing data (each test's download), not the VM. A full WiFi test downloads up to 400 MB, and a mobile one up to 75 MB. It's paid from the Google Cloud free-trial credit, which **ends on 17 Feb 2027**. After that the VM stops unless the billing account is upgraded, and tests fall back to the Cloudflare Worker automatically.
