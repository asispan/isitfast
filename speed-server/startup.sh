#!/bin/bash
# GCE startup script for the India speed server (Debian 12). Idempotent: safe on every boot.
set -euo pipefail
if ! command -v caddy >/dev/null; then
  apt-get update
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg nodejs chrony
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi
# BBR + TCP buffers sized for ~1.5 Gbps at 40 ms. The send buffer is capped at 8 MB on purpose: download
# receipts are stamped when bytes enter the socket, so a bigger buffer lets them run further ahead of
# delivery and makes the origin's receipt check more lenient.
cat > /etc/sysctl.d/90-speed.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 16777216
net.core.wmem_max = 8388608
net.ipv4.tcp_rmem = 4096 131072 16777216
net.ipv4.tcp_wmem = 4096 65536 8388608
net.ipv4.tcp_slow_start_after_idle = 0
EOF
sysctl --system >/dev/null
