# Invoice Ninja 5.13.31 native backup and restore revalidation

Research date: 2026-08-16

Scope: the existing `invoiceninja` plugin, the exact Invoice Ninja deployment
declared in `homelab-infra`, Invoice Ninja 5.13.31's first-party source and
documentation, and immutable OCI metadata for a reproducible local drill. No
production host, URL, container, database, API, or stored artifact was
contacted. Only tracked infrastructure declarations were read, and no secret
value is reproduced here.

## Decision summary

The existing plugin should be revalidated and hardened around Invoice Ninja's
native company export/import flow. Two exact local backup-to-fresh-destination
rounds are feasible without production downtime, another user decision,
privileged workload containers, host networking, published ports, or a Docker
socket inside any workload. The app image already runs PHP-FPM, its scheduler,
and two queue workers under Supervisor, so an app, MySQL, and Nginx triplet per
isolated instance is sufficient
([Supervisor contract](https://github.com/invoiceninja/dockerfiles/blob/c250510b55210789cc262719a0cfc80617889ef3/alpine/5/rootfs/etc/supervisord.conf),
[official Compose topology](https://github.com/invoiceninja/dockerfiles/blob/c250510b55210789cc262719a0cfc80617889ef3/docker-compose.yml)).

The honest restore declaration must remain `restore_capability = "partial"`.
That is not merely because `POST /api/v1/import_json` returns before its queued
job finishes. Version 5.13.31 has four material recovery boundaries:

1. The API exposes queue acceptance but no terminal import-status resource.
   The job can catch a data-import exception after destructive purge, log it,
   and continue cleanup without surfacing that failure through the API
   ([import controller](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Http/Controllers/ImportJsonController.php),
   [import job](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Jobs/Company/CompanyImport.php)).
2. The import restores selected-company settings and business data into the
   destination's existing account; it deliberately regenerates instance-bound
   company identity and does not import exported company API tokens or system
   logs. Environment, `APP_KEY`, database-server state, and other instance
   configuration are outside this artifact.
3. Export is a sequence of live ORM reads and file reads, without a database
   transaction, application write lock, or source snapshot. It is the vendor's
   supported logical backup, but it is not a provably point-in-time-consistent
   backup while users or integrations are writing
   ([company export job](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Jobs/Company/CompanyExport.php)).
4. Most importantly, the 5.13.31 importer does not reliably restore embedded
   document bytes into a fresh isolated private-network destination. For every
   document absent from destination storage, it first constructs a URL from
   the source archive's `storage_url`. Its SSRF guard rejects the source
   container's private address and then `continue`s; it never falls through to
   the embedded `documents/<url>` member. Pre-seeding destination paths or
   giving a local source a public-routable identity would make the test
   non-fresh or distort the supported topology, so neither is an acceptable
   workaround
   ([document import branch](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Jobs/Company/CompanyImport.php#L1532-L1625),
   [remote-URL guard](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Jobs/Company/CompanyImport.php#L1632-L1658)).

Therefore two exact local rounds can completely prove backup publication and
can prove restored company, customer, and invoice markers. They can also prove
that each document marker and its exact bytes are present in the source API and
in the exported ZIP. They **cannot honestly pass document-byte restore on a
fresh private destination using 5.13.31's supported native importer**. The
drill should retain this as an explicit expected partial boundary and must not
claim a full restore. A full document restore needs an upstream importer fix or
a separately researched composite recovery contract.

## Exact deployed topology and provenance

The tracked deployment inspected at `homelab-infra` commit
`eeed77a76fbc23db3da8470011535ad64cf0bc75` is
[`docker.compose/work/invoiceninja/invoiceninja.yaml`](../../../homelab-infra/docker.compose/work/invoiceninja/invoiceninja.yaml):

| Component | Declared topology |
| --- | --- |
| Application | `invoiceninja/invoiceninja:5`, container `invoiceninja`, 768 MiB limit |
| Database | `mysql:8.4.0-oraclelinux8`, container `invoiceninja-mysql`, 640 MiB limit |
| HTTP | `nginx:1.31.3`, container `invoiceninja-nginx`, port `8980:80` |
| Persistent app data | host `public` and `storage` paths mounted read-write into the app; `public` mounted read-only into Nginx |
| Persistent DB data | host MySQL data path mounted at `/var/lib/mysql` |
| Network | private `invoiceninja_network` shared by the three services |

Only environment-variable names were inspected. The declaration uses an
external environment file for `APP_KEY`, database credentials, initial user,
mail, and other settings; those values are not part of this research. The
tracked Homelab Backup backend is not attached to `invoiceninja_network`, so a
future production target should use the already routed HTTP origin rather than
adding a Docker socket or broad filesystem mount
([Homelab Backup declaration](../../../homelab-infra/docker.compose/system/homelab-backup/homelab-backup.yaml),
[declared Invoice Ninja routing](../../../homelab-infra/files/pfsense/haproxy-services.yaml)).

Invoice Ninja release `v5.13.31` is commit
`fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2`. The official release is signed and
was published on 2026-08-12
([v5.13.31 release](https://github.com/invoiceninja/invoiceninja/releases/tag/v5.13.31)).
The official Dockerfiles tag `5.13.31` is commit
`c250510b55210789cc262719a0cfc80617889ef3`. Its Alpine v5 image embeds that app
version, runs as UID 1500, and starts `supervisord`
([Dockerfile](https://github.com/invoiceninja/dockerfiles/blob/c250510b55210789cc262719a0cfc80617889ef3/alpine/5/Dockerfile),
[entrypoint](https://github.com/invoiceninja/dockerfiles/blob/c250510b55210789cc262719a0cfc80617889ef3/alpine/5/rootfs/usr/local/bin/docker-entrypoint)).

Read-only OCI registry resolution on the research date produced these exact
Linux/amd64 pins:

| Image | OCI index | Linux/amd64 manifest | Source revision |
| --- | --- | --- | --- |
| `invoiceninja/invoiceninja:5.13.31` | `sha256:c0526fc0242f4bd145d0d225bb40e174841f67597a57215723f20f2c00c0698f` | `sha256:5c051fd2a7914b05deb759556ba1a7959a86a22a8ffff488267f7cdd00713217` | app `fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2`; Dockerfiles `c250510b55210789cc262719a0cfc80617889ef3` |
| `mysql:8.4.0-oraclelinux8` | `sha256:f7a8e140a7d6d1e6e0c99eeb0489c50a186ee4ac44ff55323a176529b9a43d33` | `sha256:53a71a83be1fcbb1489c0fe23d377297f55b77a6c6ce816ca8fa30225adfe2df` | `docker-library/mysql` `c05422492215b3f0602409288c868ee4fd606ac3` |
| `nginx:1.31.3` | `sha256:8541484afbc9c8a5a8a99b379568ebbc957f658583ec9448fc43104229c03cf8` | `sha256:963cfe6e75d1c292f66589d7e190b137cf89310414c0c1c5b476dfc61a4fcd0d` | `nginx/docker-nginx` `ccdab6c99ae2e2fc53a144dc68d6b8f44163adf2` |

Use the platform manifests, not the mutable deployment tags, in the local
contract. The official image metadata identifies version 5.13.31, exposes FPM
on port 9000, runs as UID 1500, and starts Supervisor; the first-party
Supervisor configuration starts two one-attempt database queue workers with a
3600-second worker timeout. The application export job itself declares a
21600-second timeout, while its signed download is retained for one hour, or
five hours above 10,000 activities
([Docker Hub image](https://hub.docker.com/r/invoiceninja/invoiceninja/tags),
[export controller](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Http/Controllers/ExportController.php),
[export job](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Jobs/Company/CompanyExport.php)).

## Native API contract

### Non-destructive connectivity and version check

`GET /api/v1/ping`, authenticated with `X-API-TOKEN` and
`X-Requested-With: XMLHttpRequest`, is the correct non-destructive check. It
only reads the authenticated user's current company and user display names and
returns them as `company_name` and `user_name`; the global response middleware
also emits `X-APP-VERSION`
([ping controller](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Http/Controllers/PingController.php),
[API authentication headers](https://invoiceninja.github.io/docs/developer-guide)).

For the 5.13.31-scoped milestone, `test()` and `get_status()` should require a
2xx response, a non-empty string for both response fields, and exact
`X-APP-VERSION: 5.13.31`. `get_status()` must execute this real check rather
than return a constant. Neither method should log or return the token.

### Asynchronous export

`POST /api/v1/export` requires the normal API headers. It immediately creates a
UUID hash, caches a temporary protected-download URL, dispatches
`CompanyExport`, and returns HTTP 200 with `{"message":"Processing","url":...}`.
Before the job finishes, `GET /api/v1/protected_download/<hash>` returns 404
because the cached value is not yet a stored object. When the queue job
finishes, it replaces the cache value with the ZIP's storage path and the same
GET streams the file. The URL is its own temporary credential; the plugin
correctly must not send the API token to it
([export controller](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Http/Controllers/ExportController.php),
[protected download](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Http/Controllers/ProtectedDownloadController.php)).

The exact local 5.13.31 probe refined that source-level description. The
returned absolute same-origin URL has path
`/api/v1/protected_download/<UUID>` and exactly two nonempty query parameters,
`expires` and `signature`. Their values are temporary credentials and must
never be logged. During the observed queue window the endpoint returned a 200
HTML error page before returning the final ZIP, rather than the source-level
404 expectation. Polling must therefore require the exact signed URL shape and
a valid bounded ZIP response, not infer readiness from HTTP status alone.

The returned URL should remain on the configured canonical origin. Same-origin
backup attempts must share an async lock so overlapping jobs cannot create and
mistake one another's exports. The poll must accept only a bounded ZIP/binary
response, stop on authorization expiry, and stay inside a timeout shorter than
the vendor URL retention. The current default 55-minute window is suitable for
the ordinary one-hour URL, but a validated optional longer timeout may be
needed for companies whose activity count selects the five-hour path.

### Asynchronous import

`POST /api/v1/import_json` is admin-only. The ordinary request is multipart
with a ZIP in field `files` and string flags `import_settings=true` and
`import_data=true`. The controller stores the upload under a randomized name,
dispatches `CompanyImport`, and immediately returns HTTP 200 with
`{"message":"Processing","success":true}`. The importer then performs
preflight, overwrites selected-company settings, destructively purges business
data, imports the company and entity graph, and deletes the uploaded migration
file
([request validation](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Http/Requests/Import/ImportJsonRequest.php),
[import controller](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Http/Controllers/ImportJsonController.php),
[import job](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Jobs/Company/CompanyImport.php)).

Invoice Ninja's user guide explicitly says JSON import overwrites the currently
selected company. Restore must therefore be disabled by default, require the
repository's isolated-restore opt-in and exact canonical-origin allowlist,
reject a destination matching the source origin, and refuse a destination that
already contains user business data
([official import/export guidance](https://invoiceninja.github.io/docs/user-guide/basic-settings)).

There is no vendor terminal-status endpoint to poll. The plugin can poll exact
application markers after queue acceptance, but this is evidence about the
selected records, not proof that every imported entity succeeded. Its result
must remain `partial`, and a timed-out or mismatched destination must fail.

## Authoritative export artifact

The root archive members written by 5.13.31 are:

- `backup.json`, the selected company's logical state;
- `company_logo.png`;
- `documents/<document.url>` for document bytes the exporter could read; and
- `backups/<backup.filename>` for native Backup-model files it could read.

`backup.json` contains scalar/object keys `app_version`, `storage_url`, and
`company`, plus arrays for `activities`, `backups`, `users`,
`client_contacts`, `client_gateway_tokens`, `clients`, `company_gateways`,
`company_tokens`, `company_ledger`, `company_users`, `credits`,
`credit_invitations`, `designs`, `documents`, `expense_categories`, `expenses`,
`group_settings`, `invoices`, `invoice_invitations`, `payment_terms`,
`payments`, `products`, `projects`, `quotes`, `quote_invitations`,
`recurring_expenses`, `recurring_invoices`, `recurring_invoice_invitations`,
`subscriptions`, `system_logs`, `tasks`, `task_statuses`, `tax_rates`,
`vendors`, `vendor_contacts`, `webhooks`, `purchase_orders`,
`purchase_order_invitations`, `bank_integrations`, `bank_transactions`,
`schedulers`, `e_invoicing_tokens`, and `locations`
([exact exporter](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Jobs/Company/CompanyExport.php)).

The artifact is highly secret-bearing. The exporter makes contact authentication
fields visible, includes company tokens, writes decrypted gateway configuration,
and on self-hosted systems exports `EInvoicingToken::all()` rather than a
company-scoped query. Artifacts and temporary files must remain owner-private;
tokens, rows, marker payloads, internal URLs, and filenames derived from user
data must never appear in logs, metrics, sidecars, or exception messages.

The current plugin's validation—non-empty CRC-clean ZIP plus a JSON object named
`backup.json`—is not enough for dependable recovery. The milestone should add
bounded member count, per-member and total-uncompressed limits, compression
ratio bounds, no duplicate or ambiguous normalized names, no absolute/traversal
paths, CRC verification, no trailing data, exact `app_version`, the required
object/array types above, and safe document paths. For each `documents` record
that describes locally stored bytes, require the corresponding embedded member
and validate its declared size where trustworthy. The vendor exporter catches
document and native-backup read errors and silently continues, so a ZIP can be
structurally valid while omitting files; this is exactly why the plugin must
check the mapping rather than trust ZIP validity alone.

The plugin cannot manufacture a database consistency boundary around this HTTP
export. It can serialize its own same-origin backups, record source check times,
and validate the finished graph, but it must document that concurrent user or
integration writes can race the sequential export. A database/filesystem
snapshot with stronger consistency is a different composite capability and is
not part of this focused milestone.

## Phase-distinct supported-API markers

Use random, non-sensitive run identifiers. Never use real names, addresses,
invoice details, or credentials. The official API is `/api/v1`, authenticates
with `X-API-TOKEN`, and uses hashed string IDs in responses
([developer guide](https://invoiceninja.github.io/docs/developer-guide)). The
official container can bootstrap one disposable admin account from
`IN_USER_EMAIL` and `IN_PASSWORD`; its first-run command creates the company,
owner, and a system API token
([first-run script](https://github.com/invoiceninja/dockerfiles/blob/c250510b55210789cc262719a0cfc80617889ef3/alpine/5/rootfs/docker-entrypoint-init.d/10-init-in.sh),
[account creation](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/app/Console/Commands/CreateAccount.php)).
Log in through `POST /api/v1/login`; use the returned system token only in
ephemeral drill memory/files outside the repository.

For each phase `A` and `B`:

1. **Company marker:** read the current company through
   `GET /api/v1/companies/{id}`, preserve its complete settings object, change
   only `settings.name` to a unique phase marker, and PUT the complete object to
   `/api/v1/companies/{id}`. Sending only `settings.name` is unsafe because the
   settings saver rebuilds defaults. Prove the marker through both the company
   response and `/api/v1/ping`.
2. **Customer marker:** `POST /api/v1/clients` with a unique synthetic `name`,
   `id_number`, and phase-specific fake contact email. Record the returned
   hashed client ID and prove it through a filtered `GET /api/v1/clients`
   including contacts
   ([official client API](https://invoiceninja.github.io/docs/developer-guide/api/clients)).
3. **Invoice marker:** `POST /api/v1/invoices` with that client ID, a unique
   number and private/public note, and a single line item whose product key and
   notes contain the phase marker. Record the returned hashed invoice ID and
   prove the exact client relationship, number, notes, and line item with
   `GET /api/v1/invoices/{id}`
   ([official invoice API](https://invoiceninja.github.io/docs/developer-guide/api/invoices)).
4. **Document marker:** upload a tiny phase-specific `.txt` payload using
   multipart `PUT /api/v1/invoices/{id}/upload` with `documents[]`. Record the
   returned document ID, prove its metadata through the invoice/documents API,
   download it through `GET /api/v1/documents/{id}/download`, and compare exact
   bytes and SHA-256. The same bytes must exist at the archive member referenced
   by the document's `backup.json` record. The supported upload/download routes
   are declared in the first-party API router
   ([API routes](https://github.com/invoiceninja/invoiceninja/blob/fc469d040a9a9533d14b5cec7f0fc26ed2fb40c2/routes/api.php)).

Phase separation should be temporal, not just differently named objects:

- create phase A markers, take artifact A, and record its independent sidecar;
- create phase B markers, take artifact B, and require a different artifact
  path, byte size or digest, export timestamp, and marker graph;
- restore A into a brand-new destination A and require A company/client/invoice
  markers while proving B markers absent;
- restore B into a separately brand-new destination B and require both A and B
  company history where applicable, client, and invoice markers; and
- on both destinations, poll with a hard deadline and prove that document
  metadata/bytes are not silently claimed as restored. Under the exact native
  5.13.31 contract the private-network document byte check is expected to
  expose the partial boundary described above.

Because the company has only one current name, artifact A must contain the A
company name and artifact B the B company name; destination A must end with A
and destination B with B. Clients and invoices accumulate, so A contains only
A while B contains A and B. Document archive members follow the same cumulative
pattern even though native destination import cannot be credited with restoring
their bytes.

## Exact local drill topology and gates

Use one source triplet and two sequentially created, independently fresh
destination triplets. Each triplet has:

- Invoice Ninja pinned to the exact Linux/amd64 manifest above;
- MySQL pinned to its exact Linux/amd64 manifest with a private named volume;
- Nginx pinned to its exact Linux/amd64 manifest and the first-party vhost;
- independent named volumes for `/var/www/app/public`,
  `/var/www/app/storage`, and `/var/lib/mysql`;
- a private per-drill Docker network with no published ports; and
- ephemeral, randomly generated `APP_KEY`, DB credentials, owner password, and
  owner email supplied outside the repository.

Use the image's real database-backed queue and bundled queue workers, not
`QUEUE_CONNECTION=sync`, because the feature under test is asynchronous. A
small runner/backend container may join only the drill network to exercise the
plugin and API. Do not use privileged mode, host PID/network namespaces, broad
host mounts, or a Docker socket inside any Invoice Ninja, Nginx, MySQL, or
runner container. The host-side integration harness may use the already
approved local Docker engine solely to create and remove resources with a
unique test label/prefix.

Each round must prove:

- exact image identity and `X-APP-VERSION: 5.13.31`;
- successful non-destructive `test()`;
- a unique validated private artifact under the normal target/date path;
- an owner-private sidecar whose independently recomputed size and SHA-256
  match the artifact;
- artifact A/B phase distinction and exact JSON/document marker membership;
- rejection of cross-origin export URLs, corrupt/truncated/oversized ZIPs,
  wrong app versions, missing document members, expired signed downloads, bad
  credentials, same source/destination, unauthorized restore origins, and a
  non-fresh destination;
- import queue acceptance followed by exact application polling for the
  company, client, and invoice contract;
- the exact expected document restore limitation, without reporting it as
  success; and
- cleanup of every labeled container, network, and named volume even after
  failure or cancellation.

This work is fully executable on the dev VM as a **partial native recovery
milestone**. It is not fully executable as a full document-bearing recovery
milestone on Invoice Ninja 5.13.31 without changing upstream behavior or
adopting a separately approved composite restore design.

## Implementation consequences for the existing plugin

The implementation milestone should stay focused:

- scope the adapter to exact 5.13.31 and reject version drift;
- make `test()` and `get_status()` real, non-destructive, and redacted;
- canonicalize origins, serialize same-origin backup/restore operations, and
  reject source-equals-destination;
- hold the verified artifact descriptor through multipart upload so path
  replacement cannot swap bytes after validation;
- add strict bounded archive and `backup.json` validation, including document
  membership and exact phase markers;
- gate restore behind the isolated local allowlist and a precise fresh-company
  preflight;
- poll supported read APIs after import acceptance, with cancellation-safe
  cleanup and explicit partial evidence; and
- keep `restore_capability = "partial"`, with documentation that identifies
  async opacity, selected-company scope, live-export consistency, and the
  5.13.31 document-import defect.

Do not add a silent filesystem copy, shared source/destination volume,
public-address trick, database mutation, or direct MySQL restore to make the
native drill green. Each would be a different recovery contract and would hide
the exact limitation this revalidation is supposed to surface.
