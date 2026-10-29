#!/usr/bin/env bash
# Smoke test of the container image (waldonso2/wealthfolio-importer-addon#148), as CI runs it:
#   scripts/docker_smoke.sh <image>
# Without its key the container must not start; with it, hardened as in deploy/docker-compose.example.yml, the UI
# answers over HTTPS, the redirect over HTTP, the setup code is in the log and data/ holds only sealed files.
set -euo pipefail
IMAGE="${1:?image}"
WORK="$(mktemp -d)"
NAME="companion-smoke-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm -f "$NAME-data" "$NAME-tls" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

set +e
docker run --rm --read-only --cap-drop ALL "$IMAGE" >"$WORK/nokey.log" 2>&1
code=$?
set -e
[ "$code" = 2 ] || { cat "$WORK/nokey.log"; echo "without its key the container must stop with 2, not $code"; exit 1; }
grep -q "Schlüssel des Dienstes fehlt" "$WORK/nokey.log"

openssl rand -base64 32 >"$WORK/companion.key"
chmod 644 "$WORK/companion.key"  # the container's user reads the bind-mounted secret
docker run -d --name "$NAME" --hostname companion --read-only --tmpfs /tmp:size=64M,mode=1777 --cap-drop ALL \
  --security-opt no-new-privileges:true -v "$NAME-data:/data" -v "$NAME-tls:/tls" \
  -v "$WORK/companion.key:/run/secrets/companion-key:ro" -p 127.0.0.1:18443:8443 -p 127.0.0.1:18090:8090 \
  "$IMAGE" >/dev/null
for _ in $(seq 1 60); do curl -fsk -o /dev/null https://127.0.0.1:18443/healthz && break; sleep 1; done
curl -fsk -o /dev/null https://127.0.0.1:18443/healthz
[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18090/)" = 308 ]
[ "$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:18443/)" = 303 ]
docker logs "$NAME" 2>&1 | grep -q "Setup code"
docker exec "$NAME" wealthfolio-companion security-status | grep -q "Secret des Containers"
docker exec "$NAME" sh -c 'grep -lE "wealthfolio|password|brokers" /data/* 2>/dev/null' && { echo "readable data in /data"; exit 1; }
echo "container smoke test passed"
