# Community Pages API fork

Based on upstream `v1.4.2` (`5f7d92784c403f76284f0f16718f320221dc7fec`).
The backend and live Dockerfiles extend the exact upstream image digests used
by the existing installation. No dependencies, database models, migrations,
frontend assets, or storage paths change.

## API and SDK

Fourteen API-key authenticated operations are exposed at:

- `/api/v1/workspaces/{slug}/pages/`
- `/api/v1/workspaces/{slug}/pages/{page_id}/`
- `/api/v1/workspaces/{slug}/pages/{page_id}/archive/`
- The same routes under `/workspaces/{slug}/projects/{project_id}/`.

Collections use GET/POST, details GET/PUT/DELETE, and archive POST/DELETE.
PUT accepts a name, HTML, or both. Creation requires name and description_html.
Lists include archived Pages and use Plane cursor pagination (20 default, 100
maximum). Workspace routes address global Pages (`is_global=true`); existing
project Pages are accessed through the project routes. Active membership,
private-page ownership, and guest feature access are enforced. Guests cannot
write. Archive, unarchive, and deletion require owner/admin access. Deletion
requires prior archiving. Pages with child Pages reject archive/delete until
the children are explicitly moved or removed; descendants are never silently
changed.

The [Plane CLI/SDK](https://github.com/EduSantosBrito/plane-cli) has the matching
routes. Set PLANE_API_URL to the instance URL and use its existing API key.
Work-item Page associations remain Cloud-only and are outside this extension.

## Collaborative editor

Deploy **both** the backend and live images. This version supports **one live
instance**, matching the existing installation. The API takes the same Redis
mutex that live holds from document loading through final store/unload.
API writes return 409 while an editor has a Page loaded. Close the Page and
wait for unload before retrying. Do not automatically retry mutating requests.
The API synchronously snapshots the previous HTML/JSON/Yjs document as a Page
version. HTML or name changes invalidate the binary document (which also
contains the title); the existing live loader rebuilds it from the current
HTML and name on the next open.

Locks use Redis DB **15**, separate from the Django cache DB cleared during
API startup. Both services accept PAGE_API_LOCK_REDIS_DB to override this
number; use a dedicated database on the same Redis server. Preserve it when
clearing caches. Locks deliberately have no expiry: after a process crash,
a stale lock blocks writes rather than risking lost content. To recover,
stop **both API and live**, ensure both are fully stopped, remove only stale
`plane:page-write:*` keys from DB 15, then restart both. Never clear locks while
a live instance or API write is running. The regular Redis volume is retained.

## Build and validation

From the repository root:

```sh
revision=$(git rev-parse HEAD)
docker build -f deployments/pages-api/Dockerfile.backend --build-arg REVISION="$revision" -t edusantosbrito/plane-backend:v1.4.2-pages.1 .
docker build -f deployments/pages-api/Dockerfile.live --build-arg REVISION="$revision" -t edusantosbrito/plane-live:v1.4.2-pages.1 .
```

The backend tests run against a disposable PostgreSQL/Redis network, never the
production network. Dockerfile.test installs the test runner; mount apps/api
at /code to run the checked-out tests. Its default DB/Redis hostnames are
pages-test-db and pages-test-redis. The live image's build runs tsc; its
PageApiLock tests use vitest. The SDK has an opt-in integration test that
creates and deletes Pages and must use a disposable database.

## Existing server deployment

The NixOS module `/home/host/nixos-dotfiles/modules/plane.nix` exposes
services.plane.backendImage and services.plane.liveImage. The server config
selects the two images above. For the immediate cutover, build only its new
planectl derivation and install a systemd ExecStart drop-in using that command.
This avoids activating unrelated local NixOS edits. Keep the new derivation
as a GC root. The module configuration takes over on the next full rebuild.
The drop-in and its runtime start script live in `/var/lib/plane/pages-api/`.
The existing API, worker, beat-worker, and migrator share the backend image;
other services keep their original images. Existing named volumes are reused.

Before changing running services:

1. Validate a restored PostgreSQL backup with the SDK.
2. Stop API/live/workers, allowing live to finish storing its documents.
3. Back up PostgreSQL, uploads and the private environment file.
4. Save a manifest of existing records/content and the original compose file.
5. Recreate application containers using the fork; no volumes are removed.
6. Check HTTP health, API-key Pages reads, the manifest and volume identities.

Keep backup files root-readable only. Do not commit the environment or token
files. `/var/lib/plane/pages-api/rollback.sh` restores the previous compose
images and ExecStart behavior without changing the database or volumes.
The matching SDK modifications are additive, so reverting backend/live images
does not require reverting database migrations. A data restore is a separate
recovery operation; do not overwrite live data just to revert application code.
