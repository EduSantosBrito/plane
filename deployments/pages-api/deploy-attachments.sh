#!/usr/bin/env bash
# Run as root on the existing NixOS host after building and testing the image.
set -Eeuo pipefail
umask 077
runtime=/var/lib/plane/pages-api
image="${PLANE_BACKEND_IMAGE:-edusantosbrito/plane-backend:v1.4.2-pages.3}"
test_db="${PLANE_TEST_DB_CONTAINER:-pages-test-db}"
test_network="${PLANE_TEST_NETWORK:-plane-routes-tests}"
repo="$(cd "$(dirname "$0")/../.." && pwd)"
backup="/var/backups/plane/attachments-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup"
cp -a "$runtime/planectl" "$backup/planectl"
cp "$runtime/start.sh" "$backup/start.sh"
cp /var/lib/plane/plane.env "$backup/plane.env"
if [ -f "$runtime/attachments.compose.yml" ]; then
  cp "$runtime/attachments.compose.yml" "$backup/attachments.compose.yml"
fi
base_compose="$(python3 - "$runtime/planectl" <<'PY'
import pathlib, re, sys
text = pathlib.Path(sys.argv[1]).read_text()
print(re.search(r'-f\s+(\S+)', text).group(1))
PY
)"
cp "$base_compose" "$backup/base-compose.yml"
cp "$repo/deployments/pages-api/record_fingerprints.py" "$backup/record_fingerprints.py"
printf '%s\n' "$backup" > "$runtime/attachments-latest-backup"

rollback() {
  status=$?
  trap - ERR
  echo 'Deployment failed; restoring previous application configuration (database retained).' >&2
  cp -a --remove-destination "$backup/planectl" "$runtime/planectl"
  cp "$backup/start.sh" "$runtime/start.sh"
  if [ -f "$backup/attachments.compose.yml" ]; then
    cp "$backup/attachments.compose.yml" "$runtime/attachments.compose.yml"
  else
    python3 -c 'from pathlib import Path; Path("/var/lib/plane/pages-api/attachments.compose.yml").unlink(missing_ok=True)'
  fi
  "$runtime/planectl" up -d --no-deps api worker beat-worker live
  "$runtime/planectl" start proxy
  exit "$status"
}
trap rollback ERR

volume_manifest() {
  docker inspect plane-plane-db-1 plane-plane-minio-1 --format '{{.Name}} {{json .Mounts}}' | python3 -c '
import json, sys
rows = {}
for line in sys.stdin:
    name, mounts = line.split(" ", 1)
    rows[name] = sorted(json.loads(mounts), key=lambda mount: mount["Destination"])
print(json.dumps(rows, sort_keys=True, indent=2))'
}
volume_manifest > "$backup/volumes-before.txt"
# Disconnect editors, let debounced stores settle, then stop writers.
"$runtime/planectl" stop -t 30 proxy
sleep 15
"$runtime/planectl" stop -t 45 live
"$runtime/planectl" stop -t 45 api worker beat-worker

docker exec plane-plane-db-1 pg_dump -h /var/run/postgresql -U plane -Fc plane > "$backup/database.dump"
docker cp plane-plane-minio-1:/export - | gzip -c > "$backup/uploads.tar.gz"
docker exec -i plane-plane-db-1 pg_restore -l < "$backup/database.dump" > "$backup/database-toc.txt"
gzip -t "$backup/uploads.tar.gz"
sha256sum "$backup/database.dump" "$backup/uploads.tar.gz" > "$backup/SHA256SUMS"
fingerprint() {
  docker run --rm --network plane --env-file /var/lib/plane/plane.env --entrypoint python \
    -v "$backup/record_fingerprints.py:/tmp/record_fingerprints.py:ro" "$image" /tmp/record_fingerprints.py
}
fingerprint > "$backup/records-before.json"
# Restore the full dump on the disposable test network before migrating production.
docker exec "$test_db" createdb -U plane attachment_restore_validation
docker exec -i "$test_db" pg_restore -U plane -d attachment_restore_validation < "$backup/database.dump"
docker run --rm --network "$test_network" --entrypoint python \
  -e DJANGO_SETTINGS_MODULE=plane.settings.test \
  -e DATABASE_URL=postgresql://plane:plane@pages-test-db:5432/attachment_restore_validation \
  -e REDIS_URL=redis://pages-test-redis:6379/0 -e SECRET_KEY=isolated-restore-validation \
  -e AMQP_URL=memory:// "$image" manage.py migrate --noinput
docker run --rm --network "$test_network" --entrypoint python \
  -e DATABASE_URL=postgresql://plane:plane@pages-test-db:5432/attachment_restore_validation \
  -v "$backup/record_fingerprints.py:/tmp/record_fingerprints.py:ro" \
  "$image" /tmp/record_fingerprints.py > "$backup/records-restored-migrated.json"
cmp "$backup/records-before.json" "$backup/records-restored-migrated.json"
docker exec "$test_db" dropdb -U plane attachment_restore_validation
echo 'PASS: full backup restored and migrated on the isolated test network.'

cat > "$runtime/attachments.compose.yml" <<EOF
services:
  api:
    image: $image
  worker:
    image: $image
  beat-worker:
    image: $image
  migrator:
    image: $image
EOF
# Replace the symlink, keeping its original target in the backup.
cat > "$runtime/planectl.new" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec /run/current-system/sw/bin/docker compose --project-name plane \\
  -f $base_compose -f $runtime/attachments.compose.yml \\
  --env-file /var/lib/plane/plane.env "\$@"
EOF
chmod 0700 "$runtime/planectl.new"
mv -T "$runtime/planectl.new" "$runtime/planectl"
# The systemd drop-in already calls this path. Preserve health/migrator checks.
python3 - "$runtime/start.sh" <<'PY'
from pathlib import Path
import re, sys
p = Path(sys.argv[1])
p.write_text(re.sub(r'/nix/store/[^\s"()]+-planectl/bin/planectl', '/var/lib/plane/pages-api/planectl', p.read_text()))
PY

echo 'Applying additive migration:'
"$runtime/planectl" run --rm --no-deps --entrypoint python migrator manage.py migrate --noinput
fingerprint > "$backup/records-after-migration.json"
cmp "$backup/records-before.json" "$backup/records-after-migration.json"
echo 'PASS: every existing workflow row is unchanged after migration.'
"$runtime/planectl" up -d --no-deps api worker beat-worker
"$runtime/planectl" start live
ready=0
for attempt in $(seq 1 45); do
  if docker exec plane-api-1 python -c 'import requests; assert requests.get("http://127.0.0.1:8000/", timeout=2).status_code < 500' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
[ "$ready" = 1 ]
fingerprint > "$backup/records-after-restart.json"
cmp "$backup/records-before.json" "$backup/records-after-restart.json"
volume_manifest > "$backup/volumes-after.txt"
cmp "$backup/volumes-before.txt" "$backup/volumes-after.txt"
"$runtime/planectl" start proxy
ready=0
for attempt in $(seq 1 30); do
  if curl --fail --silent --output /dev/null http://127.0.0.1:8083/; then
    ready=1
    break
  fi
  sleep 2
done
[ "$ready" = 1 ]
trap - ERR
printf 'PASS: deployment healthy; workflow rows and volumes unchanged. Backup: %s\n' "$backup"
