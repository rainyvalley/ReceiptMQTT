#!/usr/bin/env bash
# Make a self-signed certificate for the control panel's HTTPS.
# Usage: scripts/make-cert.sh [directory] [host name or IP ...]
# Default directory: ./certs. Writes panel.crt and panel.key there.
set -euo pipefail

dir="${1:-certs}"
[ $# -gt 0 ] && shift
names=("${@:-localhost}")
san="DNS:localhost,IP:127.0.0.1"
for n in "${names[@]}"; do
  if [[ "$n" =~ ^[0-9.]+$ || "$n" == *:* ]]; then san+=",IP:$n"; else san+=",DNS:$n"; fi
done

mkdir -p "$dir"
umask 077
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -subj "/CN=${names[0]}" -addext "subjectAltName=$san" \
  -keyout "$dir/panel.key" -out "$dir/panel.crt" 2>/dev/null
# The key stays private to you; the container's app user must read it too.
chmod 644 "$dir/panel.crt" "$dir/panel.key"
echo "Wrote $dir/panel.crt and $dir/panel.key (valid for: $san)"
