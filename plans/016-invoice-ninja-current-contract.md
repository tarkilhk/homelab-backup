# Plan 016: Revalidate Invoice Ninja 5.13.31 native recovery

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **State**: DONE (local)
- **Restore capability**: `partial`
- **Production status**: local work only; every production restore is forbidden
- **Fixed point**: `be9839c`

## Outcome

Repair the existing `invoiceninja` plugin around the exact supported native
company export/import flow and prove two local backup-to-independent-fresh-
destination rounds. Pin the drill to these linux/amd64 manifests:

| Runtime | Exact manifest |
| --- | --- |
| Invoice Ninja 5.13.31 | `sha256:5c051fd2a7914b05deb759556ba1a7959a86a22a8ffff488267f7cdd00713217` |
| MySQL 8.4.0 Oracle Linux 8 | `sha256:53a71a83be1fcbb1489c0fe23d377297f55b77a6c6ce816ca8fa30225adfe2df` |
| Nginx 1.31.3 | `sha256:963cfe6e75d1c292f66589d7e190b137cf89310414c0c1c5b476dfc61a4fcd0d` |

The exact vendor behavior, authoritative artifact, source revisions, topology,
markers, limitations, and STOP conditions are documented in
`plans/research/invoice-ninja.md`.

## Honest recovery boundary

The native export contains the selected company's logical graph and embedded
document bytes. It is a supported online sequential export, not a transactional
database/filesystem snapshot, so concurrent external writes remain a documented
limitation.

Restore stays `partial` because Invoice Ninja 5.13.31 exposes import queue
acceptance but no terminal job resource, regenerates instance-bound identity,
and cannot reliably restore embedded document bytes into a fresh private-network
destination. The plugin and drill must prove company, client, and invoice state,
prove document bytes in each source/export, and explicitly demonstrate rather
than hide the destination document limitation.

## Public seams

The agreed test seams are the existing plugin interface (`validate_config`,
`test`, `backup`, `restore`, and `get_status`), loader and plugin HTTP discovery,
`RestoreService`, and the opt-in exact Docker drill. Complex ZIP parsing may use
an internal validator seam only for deterministic resource-bound/malicious
fixtures that cannot be produced safely through the HTTP adapter.

Do not add another public abstraction. Keep vendor protocol, artifact
validation, and partial-restore evidence behind the existing deep plugin
interface.

## Exact plugin contract

### Configuration and status

Keep one flat clean-breaking schema with exact `base_url`, secret `token`, and
bounded `export_timeout_seconds`. Remove the token placeholder default and
reject userinfo, query, fragment, non-HTTP schemes, paths, unknown keys, type
coercion, and unsafe timeout values.

`test()` and `get_status()` perform the same real non-destructive authenticated
`GET /api/v1/ping`, require nonempty company/user fields and exact
`X-APP-VERSION: 5.13.31`, and never return or log token/company/user values.

### Backup

Canonicalize the configured origin and serialize overlapping exports from that
origin. Trigger exactly one `POST /api/v1/export`; accept only the exact
processing response and a same-origin signed download path. Never send the API
token to the signed URL. Require the exact native absolute URL shape:
`/api/v1/protected_download/<UUID>` with exactly one nonempty `expires` and
`signature` query value, and never log either value.

Poll to a bounded deadline shorter than the signed URL lifetime. Stream the
first exact ZIP response to a mode-0600 transactional artifact, validating
before publication. On expiry, auth/protocol error, timeout, cancellation, or
invalid content, close work and remove every partial artifact and sidecar.

Strict validation must bound archive/member counts, compressed and expanded
bytes, expansion ratio, JSON/document size, and path depth; reject duplicate or
case-colliding names, links/special files, absolute/traversal paths, encryption,
unsupported compression, CRC/trailer errors, and unexpected root shapes.
Parse `backup.json`, require exact `app_version`, exact object/array fields, and
safe document mappings. Every locally stored document record must have one
matching embedded regular file with validated bytes/size. Sidecars contain only
exact version, structural counts, validation outcome, and generic artifact
identity—never rows, tokens, names, URLs, user content, or private paths.

### Restore

Before any import request require:

- `HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1`;
- a canonical exact destination-origin allowlist;
- distinct source and destination origins from staged provenance;
- exact 5.13.31 ping/version; and
- a fresh disposable selected company with no client, invoice, payment,
  project, quote, expense, vendor, product, task, or document state.

Validate the RestoreService-staged size/SHA-256 and archive through one held
descriptor, and upload exactly those verified bytes. Require the precise 200
processing/success response. Derive expected company/client/invoice markers in
memory from the descriptor-validated `backup.json`; never persist marker values
in restore metadata or audit records. Poll supported read APIs to a fixed
deadline for those markers, reject mismatches and auth/version drift, and
return `partial` with a secret-safe message that names the async/document
limitations. Never claim a
terminal full import or recover document bytes through a private fallback.

## Test-first slices

1. Exact discovery/schema/config and removal of the token default.
2. Exact ping/version behavior, real `get_status`, redaction, and protocol
   failures.
3. Canonical same-origin lock, exact export trigger, and signed-URL validation.
4. Bounded polling, mode-0600 streaming publication, sidecar, and cleanup.
5. Strict archive/JSON/document-map validation with compact malicious/resource
   matrices.
6. Timeout, repeated cancellation, URL expiry, cross-origin, and overlapping
   export behavior.
7. Hard local restore authorization, source/destination refusal, and fresh
   selected-company preflight.
8. Descriptor-bound multipart upload, exact acceptance, bounded marker polling,
   and honest partial result.
9. RestoreService staging/audit plus tamper, timeout, cancellation, nonfresh,
   and marker-failure evidence.
10. Two exact-image A/B backup-to-fresh-restore rounds and representative
    integration failures.

Work one vertical red-to-green slice at a time through those public seams.

## Exact local drill

Use a private Docker network with no published ports. Create one source and two
independent destination triplets, each with exact Invoice Ninja, MySQL, and
Nginx images plus separate named public/storage/database volumes. Use the real
database queue and bundled workers. Workload and runner containers receive no
Docker socket, privileged mode, broad host mount, or host network.

Use only synthetic ephemeral credentials. Through supported APIs create phase
A company/client/invoice/document markers, prove document download bytes, and
take artifact A. Mutate to phase B, create cumulative client/invoice/document
markers, and take artifact B. Prove distinct private artifact paths, sizes,
hashes, sidecars, JSON graphs, export times, and embedded document bytes.

Restore A and B through `RestoreService` into separate fresh destinations.
Poll supported APIs and prove A/B company state and cumulative client/invoice
separation. Restart each destination and repeat readiness/state proof. Assert
the known document-byte import limitation and the `partial` audit outcome; do
not fake it green. Run the complete A/B sequence twice from clean state.

Exercise cross-origin, corrupt/wrong-version/missing-document ZIP, expired URL,
bad credentials, unauthorized/same-origin restore, and nonfresh destination
failures. Remove and audit every labeled container, network, volume, artifact,
listener, and synthetic credential after success and failure.

## Verification and completion

- focused plugin/API/RestoreService tests pass;
- two clean exact-image drill rounds pass;
- full backend and frontend gates pass;
- mypy, changed-file Black/isort, SemVer, diff, secret scan, and resource audit
  pass;
- two independent reviewers find no unresolved P0/P1 Standards or Spec issue;
- compatibility, recovery, changelog, and ledger documentation are current;
  and
- the focused milestone is committed independently and not pushed or deployed.

Mark `DONE (local)` only when every gate passes. Production remains rollout-
pending until the exact app image is pinned, a target/schedule is approved, and
a backup-only production validation succeeds.

## Exact-drill evidence

- The final focused plugin, HTTP API, scheduler, RestoreService, and non-opt-in
  drill gate passed: `178 passed, 1 skipped in 9.88s`.
- The definitive opt-in exact-image command passed all static cases plus the
  complete two-clean-round recovery drill: `4 passed in 480.52s`. Across both
  rounds it produced four distinct validated artifacts and restored them into
  four independently fresh destinations.
- Exact application checks proved phase-separated company, client/contact,
  invoice/client relationship, public/private note, and line-item state after
  import and again after exact-triplet restart. Source and archive document
  bytes were identical; fresh destinations contained no restored document
  records or bytes, preserving the documented `partial` boundary.
- Per-round signed-URL, corrupt/wrong-version/missing-document archive,
  same-origin, unauthorized, and nonfresh-destination failures all stopped
  safely. Independent prefix and label audits found no remaining containers,
  networks, volumes, or runner image.
- The complete backend suite passed `1261 passed, 10 skipped in 356.14s`; the
  frontend passed 48 tests and its production build. Changed-file Black/isort,
  application mypy, SemVer, diff, and both final Standards/Spec reviews passed
  with no actionable findings. No production system was contacted or modified.
- Production remains rollout-pending; production restore remains forbidden.

## STOP conditions

Stop rather than adding compatibility or weakening evidence if the exact app,
MySQL, Nginx, native export/import, archive graph, URL lifetime, or API markers
differ; the destination is not fresh/isolated; a full restore claim would be
needed; document recovery would require a public-address trick, source volume,
database/file copy, or private endpoint; child/network work cannot be bounded
and cleaned; production access/write/restore or source downtime would be
required; or a legacy field, alias, fallback version, or alternate format is
requested without explicit approval.
