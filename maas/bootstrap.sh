#!/usr/bin/env bash
# Runs INSIDE the maas VM (pushed and started by ./lab.sh maas up).
# Installs MAAS (region + rack controller) with the test PostgreSQL snap, creates the admin
# user and prints the admin API key on stdout. Safe to re-run: every step checks first.
#   env: MAAS_URL (http://<vm-ip>:5240/MAAS), MAAS_ADMIN_PASSWORD, MAAS_CHANNEL (default 3.7/stable)
set -euo pipefail
: "${MAAS_URL:?}" "${MAAS_ADMIN_PASSWORD:?}"
CH="${MAAS_CHANNEL:-3.7/stable}"     # maas-test-db has no 3.8 track yet, and both snaps must match
say() { printf '\033[1;34m[maas-vm] %s\033[0m\n' "$*" >&2; }

if ! swapon --show | grep -q /swapfile; then   # cushion against OOM during image sync/commissioning
  say "adding 2 GB swap"
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

for s in maas-test-db maas; do
  if snap list "$s" >/dev/null 2>&1; then say "$s already installed"
  else say "snap install $s ($CH)"; sudo snap install --channel="$CH" "$s" >&2; fi
done

MARK=/var/snap/maas/common/.lab-initialized
if [[ ! -f $MARK ]]; then
  # Production would use an external HA PostgreSQL and separate region/rack hosts.
  say "maas init region+rack"
  sudo maas init region+rack --database-uri maas-test-db:/// --maas-url "$MAAS_URL" >&2
  sudo touch "$MARK"
fi

say "waiting for the region API"
for _ in $(seq 60); do curl -sf -o /dev/null "$MAAS_URL/api/2.0/version/" && break; sleep 5; done
curl -sf -o /dev/null "$MAAS_URL/api/2.0/version/" || { say "API did not come up"; exit 1; }

if ! sudo maas apikey --username admin >/dev/null 2>&1; then
  say "creating admin user"
  # The trailing newline answers the "Import SSH keys" prompt with nothing.
  printf '\n' | sudo maas createadmin --username admin --password "$MAAS_ADMIN_PASSWORD" \
    --email admin@sre-lab.local >&2
fi

KEY="$(sudo maas apikey --username admin | head -1)"
# Log the ubuntu user's maas CLI in as profile "admin" (used by ./lab.sh maas status|machines).
maas login admin "$MAAS_URL" "$KEY" >/dev/null
echo "$KEY"
