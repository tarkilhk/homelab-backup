# Paperless-ngx 2.20.15 backup and restore research

## Decision

Use Paperless-ngx's native `document_exporter` and matching
`document_importer`. Do not compose an independent PostgreSQL dump with a live
media-directory copy: those mechanisms have no shared transaction and cannot
prove a coherent application bundle.

Implementation is blocked until the user explicitly approves a production
execution path into the existing Paperless container. The current Homelab
Backup backend has no Paperless mount, network, or Docker socket. A raw Docker
socket is host-equivalent privilege even when mounted read-only.

## Exact deployment researched

The declared deployment uses:

- `ghcr.io/paperless-ngx/paperless-ngx:2.20.15`, upstream tag commit
  `05e48b23166df7c7afe6f329b460b0511a89496c`;
- current multi-platform OCI index digest
  `sha256:6c86cad803970ea782683a8e80e7403444c5bf3cf70de63b4d3c8e87500db92f`;
- PostgreSQL 16 on the shared system cluster;
- Redis 8;
- Gotenberg 8.35; and
- Tika 3.3.1.0.

The image declaration and mounts are in
`homelab-infra/docker.compose/misc/paperless/paperless.yaml`. The tag is not
digest-pinned in infrastructure yet, so the digest must be reverified and
pinned at rollout rather than carried forward by assumption.

## Authoritative state boundary

| State | Disposition |
| --- | --- |
| Paperless PostgreSQL records | Include through the native manifest export. |
| Originals, archive PDFs, thumbnails | Include through the native exporter. |
| API tokens | Vendor intentionally excludes them; regenerate after recovery. |
| Mail/social credentials | May appear in the export; treat the artifact as sensitive. |
| `/usr/src/paperless/data` | Rebuildable index/model/application data; exclude. |
| Redis | Queue/cache/runtime state; exclude. |
| Tika and Gotenberg | Stateless conversion services; exclude. |
| `consume` | Inbox, not authoritative after successful consumption; exclude. |
| `export` | Staging area, not source state; exclude. |

Application-level export encryption remains outside this product's scope. The
artifact must be private and handled as credential-bearing data.

## Native consistency semantics

In exact v2.20.15 source, `document_exporter` holds Paperless's media lock for
the export, serializes application objects within `transaction.atomic()`, then
copies the referenced originals, archives, and thumbnails while still holding
the media lock. Consumption, archive/thumbnail updates, renames, deletes, and
imports use the same media lock.

This is the strongest supported application boundary and prevents the relevant
file mutations from racing the export. It does not create a PostgreSQL
repeatable-read snapshot across every query, so normal metadata editing should
still be avoided during the short export window. Vendor documentation likewise
recommends ensuring Paperless is not actively consuming documents.

No full service downtime is required by the native workflow. The plugin should
refuse overlapping exports, schedule away from ingestion, and fail rather than
claim consistency if it cannot establish an idle/locked native export.

Primary sources:

- [Paperless administration and backup documentation](https://docs.paperless-ngx.com/administration/)
- [v2.20.15 exporter source](https://github.com/paperless-ngx/paperless-ngx/blob/v2.20.15/src/documents/management/commands/document_exporter.py)
- [v2.20.15 importer source](https://github.com/paperless-ngx/paperless-ngx/blob/v2.20.15/src/documents/management/commands/document_importer.py)

## Proposed artifact contract

Run a fixed command equivalent to:

```text
document_exporter ../export --zip --zip-name <unique-name> --no-progress-bar
```

Download the one generated ZIP through the Docker archive API into the normal
transactional artifact helper. Validate before publication:

- non-empty regular ZIP with valid CRCs;
- safe, unique member paths and bounded member count/expanded size;
- `manifest.json` and `metadata.json`;
- exact Paperless version 2.20.15;
- every document file referenced by the manifest; and
- private artifact mode plus valid sidecar.

Remove only the unique generated staging ZIP after it has been transferred and
validated. Timeout or cancellation must stop/reap the exec and remove all
plugin-owned partial files.

## Restore contract

Restore only to a labeled disposable destination using the exact 2.20.15 image,
an empty PostgreSQL database, and empty media directories. The importer merely
warns about non-empty destinations, so the plugin must enforce freshness before
mutation.

After `document_importer` exits successfully, require:

- healthy Paperless, PostgreSQL, and Redis status;
- rebuilt and ready search index;
- expected model/document cardinalities and representative markers;
- exact original-document checksums plus archive/thumbnail presence; and
- `document_sanity_checker` with no errors.

API tokens are regenerated after recovery. Cross-version and in-place restore
are outside the contract. Production restore remains forbidden.

## Local drill

Use one disposable internal Docker Compose project with exact Paperless 2.20.15,
PostgreSQL 16, Redis 8, Gotenberg 8.35, and Tika 3.3.1.0. Seed synthetic users,
documents, tags, correspondents, document types, notes, custom fields,
workflows, and settings through Paperless interfaces.

Take artifact A, restore it to fresh destination A, mutate the source with a
second document/marker, take artifact B, and restore it to fresh destination B.
Require independent paths, sizes, hashes, manifests, file checksums, semantic
differences, service readiness, and zero-error sanity checks.

## Production execution gate

The current Docker-host backend has no Paperless mount or Docker execution
path. Joining application networks or adding database credentials is not enough
to invoke the native management command.

Choose and explicitly approve one execution design before implementation:

1. mount the raw Docker socket into the Docker-host backend, matching the
   existing NAS deployment pattern but granting host-equivalent privilege; or
2. build and operate a purpose-built, narrowly scoped Paperless export helper,
   which reduces privilege at the cost of another maintained component.

Do not replace this decision with an independently timed database/filesystem
copy or a pre-generated export that Homelab Backup did not itself execute.
