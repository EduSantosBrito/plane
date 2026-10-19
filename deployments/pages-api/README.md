# Community Pages API fork

Based on upstream `v1.4.2` (`5f7d92784c403f76284f0f16718f320221dc7fec`).
The backend and live Dockerfiles extend the exact upstream image digests used
by the existing installation. Dependencies, frontend assets and storage paths
stay unchanged. Migration `0123_issuepage` adds the explicit work-item Page
attachment table.

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
Five additional operations implement the CLI overlay contract:

- GET/POST `/workspaces/{slug}/projects/{project_id}/work-items/{issue_id}/pages/`
- GET/DELETE the same path plus `{work_item_page_id}/` (the association UUID)
- POST `/workspaces/{slug}/projects/{project_id}/work-items/{issue_id}/relations/remove/`

All are under `/api/v1/`. Associations serialize light nested Page metadata by default and use
Plane cursor pagination (`per_page` 1–100, default 20). Duplicate attach returns
the existing association with 200; a new association returns 201. Detach soft
deletes the association and returns 204; repeating it returns 404. The partial
unique constraint `issue_page_unique_live_pair` prevents duplicate active pairs.

Attachments require active workspace and project membership. Read visibility
matches project Pages, including private ownership and guest feature access.
Writing requires project role Member or Admin; a visible Page with insufficient
write permission returns 403. Invisible, archived, deleted, foreign-project and
workspace-global Pages return 404. Workspace-global Pages are excluded even if
also linked to the project. These filters apply to attach, list, retrieve and
detach, including attachments whose Page later changes scope or visibility.
Deleted work items and associations are hidden too.

The approved model deviation is additive: v1.4.2 has editor `PageLog` mentions,
not Cloud's explicit attachment model. `IssuePage` provides independent links
with `issue.issue_pages` and `page.issue_pages` reverse relations. Existing
mentions, browser routes, Page content and work-item descriptions stay intact.
The public response matches the existing CLI overlay; no SDK change is needed.
Attachments do not add editor mentions or a browser attachment UI.

Relation removal validates both work items in the workspace and membership of
the routed project. It atomically soft deletes all edges between the two items
in either stored direction. v1.4.2 normally stores a canonical edge and derives
the reverse on reads; paired legacy rows are also removed together. Missing
relations between existing work items return 204 (idempotent); missing work
items return 404. No unrelated edges are removed.

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
docker build -f deployments/pages-api/Dockerfile.backend --build-arg REVISION="$revision" -t edusantosbrito/plane-backend:v1.4.2-pages.3 .
docker build -f deployments/pages-api/Dockerfile.live --build-arg REVISION="$revision" -t edusantosbrito/plane-live:v1.4.2-pages.1 .
```

The backend tests run against a disposable PostgreSQL/Redis network, never the
production network. Dockerfile.test installs the test runner; mount apps/api
at /code to run the checked-out tests. Its default DB/Redis hostnames are
pages-test-db and pages-test-redis. The live image's build runs tsc; its
PageApiLock tests use vitest. The SDK has an opt-in integration test that
creates and deletes Pages and must use a disposable database.

## Existing server deployment

These describe the initial Pages cutover. The attachment extension below
updates the runtime wrapper to use the second backend image.

The NixOS module `/home/host/nixos-dotfiles/modules/plane.nix` exposes
services.plane.backendImage and services.plane.liveImage. The server config
selects the two images above. For the immediate cutover, build only its new
planectl derivation and install a systemd ExecStart drop-in using that command.
This avoids activating unrelated local NixOS edits. Keep the new derivation
as a GC root. The module configuration takes over on the next full rebuild.
The runtime files live in `/var/lib/plane/pages-api/`. The persistent drop-in
is installed at `/etc/systemd/system.control/plane.service.d/50-pages-api.conf`:
NixOS's `/etc/systemd/system` points into the immutable store. systemd's
control-directory drop-in overrides ExecStart on subsequent boots. Remove it
after a full NixOS rebuild has activated the matching module configuration,
so later declarative image updates are not masked. Until then, administer the
fork with `sudo /var/lib/plane/pages-api/planectl`; the system-wide planectl
still refers to the currently activated upstream system generation. Both
planectl derivations are retained as GC roots. The built fork image tags are
local Docker images; rebuild them from the pinned source if they are pruned.
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

## Deploying the attachment extension

Run `sudo bash deployments/pages-api/deploy-attachments.sh` after building and
testing the image. This script performs the backup, isolated restore validation,
migration, restart and data/volume comparisons described below. It expects the
existing isolated `pages-test-db` and `pages-test-redis` containers on
`plane-routes-tests`.

Build the backend with `deployments/pages-api/Dockerfile.backend` and tag it
`edusantosbrito/plane-backend:v1.4.2-pages.3`. Keep the existing live image.
The backend Dockerfile includes the new model, migration, views and public URLs.
Run migration checks and API tests on the isolated PostgreSQL/Redis network
before touching production. Set `AMQP_URL=memory://` for tests without RabbitMQ.

The server uses `attachments.compose.yml` under `/var/lib/plane/pages-api` to
override only API, worker, beat-worker and migrator images. Its `planectl` wrapper
combines the existing immutable compose file with that override. `start.sh` uses
this wrapper, so the existing systemd ExecStart drop-in survives reboot. A future
NixOS rebuild should incorporate the new backend image declaratively before
removing this override.

Before restart, pause proxy/live/API/workers, save a private PostgreSQL dump,
upload archive, runtime configuration and row-content hashes. Validate the dump
and upload archive. Run the new image's `manage.py migrate --noinput`, then
recreate API/workers and restart live/proxy. Verify row hashes and volume
identities before creating scratch fixtures. Do not remove volumes. Retain the
previous runtime configuration and images for rollback: old application images
ignore the additive table, so rollback should not drop it or restore live data.


## Selective Page reads

Attachment list, retrieve and attach accept these query parameters:

| Parameter | Contract |
| --- | --- |
| `with_content=true` | Legacy full nested Page, unchanged, including HTML/JSON/stripped text. |
| `sections=true` | Light Page plus `outline`, `section_count`, and whole-document plain-text `total_chars`. |
| `section=<id>` | Light Page plus `sections`; repeat at most 10 ids, returned in request order with duplicate ids collapsed. |
| `body_format=html\|text` | Representation of a requested body; defaults to HTML. Requires `with_content=true` or `section`. |
| `limit_chars=1..100000` | Per-body character cap. Requires `with_content=true` or `section`. |

Default metadata contains only id, name, audit timestamps/users, archived_at,
is_global, access and url. The canonical link is `base_host(request)` plus
`/{slug}/projects/{project_id}/pages/{page_id}`, matching the web route and its
`getRedirectionLink` implementation. HTML, JSON, stripped text and binary data
are deferred in metadata read queries; they are not serialized or parsed.

Whole-body HTML without a limit retains the legacy shape, including when
`body_format=html` is explicit. Text or capped whole-body reads instead return
light metadata plus `body`, `body_format`, `text_chars`, `total_chars`, and
`truncated`. Full text uses stored `description_stripped`. Section bodies use
source HTML slices or `strip_tags` of the same slices. `total_chars` counts the
untruncated requested representation; `text_chars` counts untruncated plain
text. Limits slice Unicode code points; capped HTML is a raw prefix, not
reserialized or repaired markup.

Each heading starts a section ending before the next heading of the same or
higher level. Its `data-id` is the id, falling back to one-based positional
`sN`. A nonempty region before the first heading is `preamble`, level 0, with
an empty title. A heading-free document always has one preamble, including an
empty document. Outline entries are in document order and expose id, level,
title and text_chars. Parent section text counts include child headings and
content, so summing outline counts double-counts children. No sub-pages are
loaded.

Conflicting switches, invalid values, repeated scalar options and unknown
section ids return field-specific 400 errors. `with_content=true` can only be
combined with body_format/limit_chars; outline rejects section/body_format/
limit_chars. Invalid attach projections render inside the transaction and
cannot leave a new association behind. `format` is not a content parameter.

Project Page list/retrieve keep their full default when no content parameter
is passed. Passing any content option uses the same mode rules as attachments;
`with_content=false` opts into metadata. Workspace Page routes are unchanged.
There is no database migration or dependency change for this extension.
`Dockerfile.backend` explicitly ships the new `plane/api/page_content.py`.

The deployment script accepts `PLANE_BACKEND_IMAGE`, `PLANE_TEST_DB_CONTAINER`
and `PLANE_TEST_NETWORK` overrides. The current content-mode deployment uses
`pages-content-db` and `plane-content-tests`; the test network aliases remain
`pages-test-db` and `pages-test-redis` inside containers. Backups include row
fingerprints for `issue_pages`, protecting existing user-approved links.
