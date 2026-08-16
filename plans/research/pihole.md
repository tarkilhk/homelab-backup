# Pi-hole 2026.07.2 Teleporter current-contract research

Research date: 2026-08-16

Scope: the existing `pihole` plugin, its tests and recorded legacy evidence,
the current `homelab-infra` Pi-hole declaration, and exact first-party Pi-hole
2026.07.2 sources and registries. No production host, endpoint, container,
credential, configuration, or data was contacted or changed. Network activity
was limited to read-only official Git, GitHub, Docker Hub, and Pi-hole sources.

## Decision summary and active STOP

The native Pi-hole v6 Teleporter API is the correct backup and restore
mechanism, but the existing plugin does not yet satisfy the current contract.
Two upstream facts require an explicit user policy decision before local
implementation:

1. **Export is not an atomic snapshot.** Exact FTL v6.7 reads `pihole.toml`,
   optional DHCP leases, and selected SQLite tables sequentially. Its database
   copier issues one `CREATE TABLE ... AS SELECT` per source table without one
   transaction spanning every table, file, and database. Pi-hole documents
   Teleporter as an archived copy, not as an atomic concurrent snapshot. The
   user must choose either an externally proved configuration/gravity/DHCP
   mutation-quiescence window or an explicitly accepted and locally proved
   two-identical-semantic-export convergence contract. This note does not
   choose between them.
2. **Pi-hole has no endpoint-scoped read-only token.** A distinct application
   password with default `webserver.api.app_sudo=false` is the narrowest
   first-party source identity: it can export Teleporter data and cannot import
   configuration. It is not globally read-only. Exact FTL routes give the same
   authenticated session access to list, action, and deletion endpoints, with
   no general app-session scope check. The user must approve that residual
   write authority or a method/path-restricting proxy that exposes only the
   required login, version, export, and logout calls.

Until both policies are selected, **stop implementation, acceptance drilling,
and production rollout**. Do not silently call one best-effort online export
consistent, reuse the full web-admin password, or weaken validation.

Once those decisions are made, the plugin can honestly remain
`restore_capability = "automatic"` for the **Teleporter-restorable Pi-hole
configuration subset only**, provided restore is create-only against a fresh,
disposable, isolated exact-image destination and proves restart plus semantic
state. It is not automatic recovery of the whole declared Pi-hole service.

## Exact image, source, Web, and API identity

The inspected `homelab-infra` revision is
`01eae07691699a7f47a3794e9095240b672aa020`. It declares the mutable tag
`pihole/pihole:2026.07.2` in
[`docker.compose/tarkilnas-system/pihole/pihole.yaml`](../../../homelab-infra/docker.compose/tarkilnas-system/pihole/pihole.yaml).
Git proves the intended declaration, not the image already running in
production.

Read-only official registry and source resolution produced:

| Property | Exact identity |
| --- | --- |
| Image | `docker.io/pihole/pihole:2026.07.2` |
| OCI index | `sha256:f7d1be836e3bc608b56d82fc9904f5a831cdfbc0dc9c6d58f94e4c985c70038b` |
| linux/amd64 manifest | `sha256:7c96327ecfb96dbc74b0a47d073dbef7d60526e0aa87519b2a2f7a0007cb5c88` |
| Exact drill reference | `pihole/pihole@sha256:7c96327ecfb96dbc74b0a47d073dbef7d60526e0aa87519b2a2f7a0007cb5c88` |
| Docker source | [`dd91b4847d97f0aac68bfefd1c108ed0627e6c68`](https://github.com/pi-hole/docker-pi-hole/commit/dd91b4847d97f0aac68bfefd1c108ed0627e6c68) |
| Core | v6.4.3, [`f47b8ede5a8e38f9c703202d324a074dbdba4ca9`](https://github.com/pi-hole/pi-hole/commit/f47b8ede5a8e38f9c703202d324a074dbdba4ca9) |
| Web | v6.6, [`b2a4078446519c58d84f199663ca9326d5d311f0`](https://github.com/pi-hole/web/commit/b2a4078446519c58d84f199663ca9326d5d311f0) |
| FTL and embedded HTTP API | v6.7, [`fa65a88f8cdef1013594d4de14108077954faea4`](https://github.com/pi-hole/FTL/commit/fa65a88f8cdef1013594d4de14108077954faea4) |
| API document | API `6.0` expressed as OpenAPI `3.0.2` in the exact FTL source |

The official
[`2026.07.0` release](https://github.com/pi-hole/docker-pi-hole/releases/tag/2026.07.0)
introduced Core v6.4.3, Web v6.6, and FTL v6.7. The official
[`2026.07.1`](https://github.com/pi-hole/docker-pi-hole/releases/tag/2026.07.1)
and
[`2026.07.2`](https://github.com/pi-hole/docker-pi-hole/releases/tag/2026.07.2)
notes contain Docker-only fixes, so the component versions are unchanged.
FTL v6.7 is also the minimum exact restore target for this contract because it
contains Pi-hole's fix for the Teleporter-related authenticated RCE documented
in the first-party
[GHSA-8j7w-m3cr-6q6x advisory](https://github.com/pi-hole/FTL/security/advisories/GHSA-8j7w-m3cr-6q6x).
The amd64 image config labels its revision as the Docker commit above and its
version as `2026.07.2`. Docker Hub exposes the platform identities through its
official
[`2026.07.2` tag resource](https://hub.docker.com/v2/repositories/pihole/pihole/tags/2026.07.2).

The clean drill must pin the amd64 manifest, not the tag. Source `test()` and
`get_status()` should authenticate, call the documented
[`GET /api/info/version`](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/docs/content/specs/info.yaml#L178-L197),
and require exact local Core, Web, FTL, and Docker versions and hashes. HTTP
cannot prove the container manifest; an exact digest pin plus Docker inspection
belongs to the local drill and later separately approved production rollout.

## Declared topology and authoritative boundary

The current declaration publishes DNS TCP/UDP 53, HTTP 53180, and HTTPS 53443;
persists `/volume1/docker/pihole` at `/etc/pihole`; and bind-mounts
`dnsmasq-99-hollinger-wildcard.conf` read-only into `/etc/dnsmasq.d`. Its
environment sets the API password, DNS listening mode, and
`misc.etc_dnsmasq_d`; runs FTL as root; and does not publish DHCP UDP 67. These
are declaration facts only. No runtime or file was inspected.

The service has three distinct recovery owners:

1. **Teleporter-restorable state**, owned by this plugin:
   - the Pi-hole FTL configuration in `etc/pihole/pihole.toml`;
   - the seven gravity management tables `group`, `adlist`,
     `adlist_by_group`, `domainlist`, `domainlist_by_group`, `client`, and
     `client_by_group`; and
   - `etc/pihole/dhcp.leases` if DHCP is actually enabled and the file exists.
2. **Declarative deployment state**, owned by `homelab-infra`: the exact image,
   ports, hostname, environment-controlled configuration, volumes, network,
   healthcheck, and restart policy. Pi-hole's official
   [Docker configuration guide](https://docs.pi-hole.net/docker/configuration/)
   states that `FTLCONF_*` values become read-only and are the source of truth;
   removing one reverts that setting to its default. An imported TOML file
   cannot replace those declarations.
3. **External mounted state**, not restored by this plugin: the declared
   read-only Hollinger dnsmasq file and any other operator-managed external
   file, DNS/network configuration, reverse proxy/TLS trust, and plaintext
   deployment credentials.

The exact v6.7 exporter also places `etc/hosts`, direct entries from
`etc/dnsmasq.d`, and a reduced `pihole-FTL.db` containing `message`,
`aliasclient`, `network`, and `network_addresses` in the ZIP. The v6 ZIP
importer processes none of those. It recognizes only TOML, DHCP leases, and
the gravity database
([export inventory](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/zip/teleporter.c#L162-L274),
[import allowlist](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/zip/teleporter.c#L579-L600)).
Query history, cache contents, logs, generated gravity domains, current API
sessions, and transient resolver/network observations are therefore not
recovery claims.

The Teleporter ZIP is secret-bearing. `pihole.toml` can contain password
hashes, an application-password hash, TOTP material, upstream/internal network
configuration, local hostnames, and other sensitive settings. The artifact and
its staging paths must be mode 0600, never logged or returned through status,
and sidecars must contain only versions, aggregate counts/hashes, and validator
identity—not raw configuration, domains, clients, leases, paths, SIDs, or
credentials.

## Exact Teleporter API and session contract

The exact first-party
[OpenAPI Teleporter specification](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/docs/content/specs/teleporter.yaml)
defines:

- `GET /api/teleporter`: authenticated synchronous response,
  `application/zip`, containing the current Teleporter export;
- `POST /api/teleporter`: authenticated `multipart/form-data` with a required
  `file` and optional `import` selection object; omitting `import` requests all
  supported members; and
- HTTP 400 for an invalid archive, 401 for no valid session, and 200 for an
  accepted import.

Use the exact v6 session flow from the first-party
[authentication specification](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/docs/content/specs/auth.yaml):

1. `POST /api/auth` with the password only in the JSON body.
2. Require a 200 JSON object with `session.valid=true`, a nonempty bounded
   `session.sid`, and positive bounded `session.validity`.
3. Send the SID only as `X-FTL-SID`. Header authentication does not require the
   cookie-oriented CSRF token.
4. `DELETE /api/auth` with that SID and require 204 before publishing a backup.

Sessions last 30 minutes by default, authenticated activity extends them, and
password/application-password changes invalidate them. Never place password or
SID in URL, query, cookie, logs, exception text, metrics, sidecars, process
arguments, or environment. Disable redirects so authentication cannot cross an
origin. Configuration must require a canonical base origin with no userinfo,
query, fragment, or path ambiguity and an explicit TLS policy. Plain HTTP is
acceptable only inside the no-published-port, private local drill network; a
production exception would require explicit approval.

### Least privilege is not fully available

The first-party application password is preferable to the shared administrator
password: it is separately generated, shown once, bypasses interactive 2FA,
and creates an identifiable app session. With the default
`webserver.api.app_sudo=false`, exact FTL rejects Teleporter import from that
session
([POST guard](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/teleporter.c#L227-L246)).

That is not a read-only role. The exact endpoint table gives an authenticated
session access to state-changing actions and delete methods without a general
scope model
([route table](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/api.c#L90-L108)).
The source credential therefore remains capable of more than authentication,
version inspection, export, and logout. The selected policy must be tested with
representative denied source mutations; if a path-restricting proxy is chosen,
all other methods and paths must be rejected before reaching Pi-hole.

Restore must never enable app sudo on a production source. A fresh local
destination may use its unique random administrator password because it is
already destroy-and-recreate disposable; any restored authentication hash must
be overridden by the destination's declared environment credential. Source and
destination credentials must differ.

## Export consistency and completion

FTL constructs the ZIP entirely in memory and returns it in one HTTP 200 body
([GET implementation](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/teleporter.c#L37-L70)).
Required-file or database-copy failures return 500. However, the generator's
final ZIP validation failure is only logged; it still returns success
([generator validation](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/zip/teleporter.c#L276-L298)).
HTTP 200, a ZIP signature, and nonempty bytes are therefore insufficient.

There is no asynchronous export job, job identifier, or later terminal status.
The complete bounded response body is the candidate artifact. Require exact
200, exact content type, a bounded `Content-Length` consistent with bytes read,
bounded download time, no redirect, EOF, strict local validation, successful
session deletion, and only then atomic publication through the repository's
artifact helper.

The consistency problem is visible in exact source. It copies each selected
SQLite table into an in-memory database with a separate statement and no
transaction around the loop, then reads other files and the second database at
different times
([database copier](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/zip/teleporter.c#L77-L159),
[export sequence](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/zip/teleporter.c#L174-L274)).
All-In-One ZIP delivery does not make those reads one recovery point.

The unresolved policies are:

- **Quiesced:** externally prevent every admin/API/gravity/DHCP mutation for
  the complete export interval, prove no competing session or scheduled
  gravity activity, then export and validate. DNS answering may remain online,
  but the mutation fence must be observable and guaranteed.
- **Convergent online:** take two complete consecutive exports, normalize only
  the restorable semantic projection, require identical canonical hashes and
  valid cross-table relationships, and publish the second. This greatly
  narrows races but is not a vendor-guaranteed atomic snapshot. Adopting it is
  an explicit risk decision, not a technical fact.

If DHCP is enabled and leases can change during the interval, or if gravity or
configuration mutation cannot be fenced/converged, stop. The current
declaration does not publish DHCP, but production runtime must not be inferred
from that alone.

## Strict artifact validation without unsafe import

Validate before publication and again from a RestoreService-staged,
descriptor-bound artifact before any destination contact. Do not ask Pi-hole
to validate an untrusted candidate—the import endpoint mutates while parsing
recognized members.

The local validator should:

- enforce conservative compressed size, total expanded size, member count,
  per-member size, path length/depth, compression ratio, and deadline limits
  below FTL's 50 MiB upload and 256 MiB recognized-member ceilings
  ([upload limit](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/teleporter.c#L120-L152),
  [expanded limit](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/zip/teleporter.c#L602-L626));
- reject encryption, unsupported compression, duplicate or case-colliding
  normalized paths, absolute/backslash/dot/traversal paths, NUL/control names,
  links, devices, FIFOs, overlapping entries, CRC failure, trailing ambiguity,
  or a local/central-directory mismatch;
- accept only the exact generated layout: one regular nonempty
  `etc/pihole/pihole.toml`, one `etc/hosts`, one
  `etc/pihole/gravity.db`, one `etc/pihole/pihole-FTL.db`, optional one
  `etc/pihole/dhcp.leases`, and bounded direct regular files under
  `etc/dnsmasq.d/`; reject all other members;
- parse TOML locally with no interpolation or command execution; bind a
  canonical hash of the restorable, environment-aware configuration while
  redacting all values from output;
- open each bounded SQLite member as immutable/query-only, disable extension
  loading and trusted schema behavior, run integrity checks, require only the
  exact expected reduced table sets, validate types and bounded counts, and
  enforce gravity relationship references; never execute archive SQL or a
  schema-supplied extension;
- require every exporter-guaranteed member even though FTL imports only a
  subset, so truncation or a partially generated archive cannot pass; and
- bind artifact size/SHA-256, exact component/image identities, normalized
  member inventory, canonical restorable-projection hash, validator version,
  and chosen consistency-policy evidence into a minimal private sidecar.

The current validator only checks that Python can read the ZIP, CRCs pass, and
`etc/pihole/pihole.toml` exists. Its test fixture is a one-member synthetic ZIP
that exact FTL could accept as a no-op/partial import. It proves neither a real
Teleporter artifact nor recoverability.

## Restore behavior, isolation, and honest capability

Exact v6.7 import is destructive and only partly transactional:

- TOML is parsed and dnsmasq-tested, then replaces the current configuration;
- DHCP leases, if present, overwrite the destination file after rotation;
- selected gravity tables are deleted and inserted inside one SQLite
  transaction; but
- no transaction spans TOML, leases, and gravity, so a later failure can leave
  earlier state changed
  ([configuration import](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/zip/teleporter.c#L301-L358),
  [recognized-member loop](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/zip/teleporter.c#L635-L775)).

The API processes the upload synchronously, returns a JSON `files` array, then
requests an FTL restart. The central dispatcher initiates the restart after the
handler returns
([POST completion](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/teleporter.c#L331-L365),
[restart dispatch](https://github.com/pi-hole/FTL/blob/fa65a88f8cdef1013594d4de14108077954faea4/src/api/api.c#L268-L279)).
The OpenAPI schema currently calls the response property `processed`, while the
exact implementation emits `files`; the exact-image drill must bind the v6.7
implementation behavior and stop on any difference. A 200 is import acceptance,
not terminal readiness.

Restore is authorized only when all of these are true:

1. RestoreService authenticated and privately staged an artifact plus matching
   Pi-hole sidecar from the named source target.
2. Source and destination IDs/origins differ; the destination matches a strict
   local drill allowlist and explicit restore-destination mode.
3. The orchestrator created a fresh exact-manifest Pi-hole container with a new
   empty volume, private internal network, no production mount, credential,
   route, DNS port, published port, or shared namespace.
4. Destination version/status is exact and its initial semantic projection is
   the known fresh sentinel. Any pre-existing user configuration stops restore.
5. The plugin posts the unmodified native ZIP with no partial `import` filter,
   requires exact 200 JSON and the complete expected `files` set, and tolerates
   connection loss only after that complete response has been parsed.
6. It obtains a new destination session after restart, requires exact component
   versions, exports and strictly validates the restored projection, and proves
   its canonical hash equals the artifact projection.
7. It verifies phase markers through supported configuration/gravity APIs and
   real DNS allow/block/CNAME behavior, restarts the whole destination container,
   and repeats readiness, version, projection, and behavior checks.

Never retry import into a partially changed destination. On any failure,
destroy and recreate it. Never import into production, an existing/shared
instance, the source, or a destination whose lifecycle the drill cannot
control.

Under those constraints, `automatic` is accurate for the Teleporter subset:
the first-party API performs the import and restart without a manual data step.
It remains incomplete service recovery until `homelab-infra` recreates the
exact environment and independently restores the external dnsmasq bind and
other deployment prerequisites.

## Exact two-clean-round Docker drill

Run the complete A/B backup-to-fresh-restore sequence twice from clean state.
Use only the pinned linux/amd64 manifest on one private internal network with no
published ports, host network, production secret/mount, Docker socket inside
workload containers, privileged mode, or external DNS route. Give every source
and destination a unique random credential and volume.

Each clean round must:

1. Start a fresh exact Pi-hole source; prove the image digest and exact
   Core/Web/FTL/Docker API identity, DNS readiness, and empty synthetic
   baseline. Create the selected source credential/control and prove its
   intended denied paths or proxy allowlist.
2. Through supported admin APIs, create phase A with a distinctive configuration
   marker, local DNS/CNAME marker, group, adlist, exact allow and deny domains,
   client-to-group relationship, and a deterministic DNS query behavior. If
   DHCP is included in the chosen contract, add a deterministic lease marker;
   otherwise prove it is absent and excluded.
3. Apply the selected consistency policy and execute a real scheduled
   Target/Job/Run/TargetRun backup. Prove private transactional publication,
   exact sidecar, independent bytes/SHA-256, member inventory, canonical
   projection, relationship validity, session cleanup, secret absence from
   logs/status/sidecar, and no source mutation by the plugin.
4. Mutate only through supported admin APIs to cumulative phase B: add distinct
   configuration, group/domain/client, and DNS behavior markers while retaining
   phase A. Take a second scheduled artifact. Prove A remains immutable and the
   A/B artifact hashes and semantic projection hashes differ.
5. Through the real RestoreService path, restore A and B into two independent
   fresh exact-manifest destinations. Require the exact import `files` response,
   observe the FTL restart transition, then prove A excludes B while B contains
   A plus B through post-export projection equality, API objects, and real DNS
   queries.
6. Restart each destination container and repeat component identity, readiness,
   semantic export, relationship, and DNS checks. Prove source and destinations
   used distinct credentials and no restored source hash displaced the declared
   destination login.
7. Tear down every container, volume, network, session, staging file, synthetic
   credential, and artifact copy labeled with the drill prefix. An injected
   failure at each phase must leave no published partial artifact and no
   reusable partial destination.

Two rounds therefore produce four distinct scheduled artifacts and four fresh
restores. Required negative cases include bad auth/rate limit, redirect, TLS
failure, wrong version/hash/digest, incomplete body, logout failure, policy
quiescence/convergence failure, malformed/duplicate/traversal/encrypted ZIP,
CRC/ratio/size/count breach, missing or extra members/tables, corrupt SQLite or
TOML, broken gravity relationships, altered artifact/sidecar, wrong source or
destination, nonfresh/same/production destination, partial import response,
restart timeout, projection mismatch, DNS mismatch, cancellation, and cleanup
failure.

## Concrete repository gaps

The current plugin and tests are a useful v6 baseline, not current-contract
proof:

- schema configuration has a fake password default, does not mark the secret
  write-only, has no source/destination mode, TLS policy, URL/network allowlist,
  version identity, consistency policy, or restore authorization;
- `validate_config()` checks only two nonempty strings;
- `test()` downloads a Teleporter but does not report exact component identity,
  session type/scope, or consistency evidence;
- `get_status()` always returns `ok` without observation;
- backup uses the shared `password` field, does not require an app session,
  treats logout failure as a warning, buffers an unbounded response, and
  validates only ZIP readability plus one filename;
- logs include the configured base URL; redirects are disabled in production
  code but test clients sometimes enable them, so credential-origin behavior is
  not proved;
- sidecars contain no exact Pi-hole identity, member/table inventory,
  restorable-projection hash, or consistency evidence;
- restore accepts any existing filesystem path and any configured Pi-hole
  origin, with no fresh-destination sentinel, same-source/production refusal,
  exact version gate, or lifecycle control;
- restore ignores the import JSON body entirely. Its mock returns
  `{"processed": true}`, which matches neither the exact implementation's
  `files` array nor the documented array schema;
- the post-import export proves only that some Pi-hole is reachable and can
  export, not that the imported artifact's configuration or gravity state was
  restored;
- the unit fixture contains only `pihole.toml`; no real exact-image export,
  gravity tables, phase distinction, DNS behavior, restart transition,
  corruption/resource case, or source privilege proof exists; and
- the compatibility document's two-export/one-restore wording is historical.
  The coverage ledger correctly keeps Pi-hole at `planned-plugin`; there is no
  retained current-contract two-round A/B-to-four-fresh-destinations evidence.

The deployed service also has a boundary gap that code cannot solve: its
read-only external Hollinger dnsmasq file is present in export but ignored on
v6 ZIP import. Full service recovery must reconstruct it from
`homelab-infra`; the Teleporter plugin must never claim otherwise.

## Production gates and STOP conditions

No production action is authorized by this research. After both open policy
decisions and the full local drill pass, production would still require
separate approval to pin/redeploy the image digest, create and store the chosen
source identity or proxy, configure TLS/network access, update the target and
schedule, and run one backup-only validation. A later approved read-only probe
must confirm exact API versions/hashes, Docker identity where safely available,
DHCP use, external state boundary, environment overrides, session type, and
transport. No production import or restore is permitted.

Stop rather than weakening the result if:

- either the consistency or source-credential policy remains undecided;
- the running image/component/API identity differs from the exact contract;
- the tag cannot be pinned to the recorded amd64 manifest;
- the source credential has unapproved mutation power or the chosen proxy can
  reach any nonallowlisted method/path;
- configuration, gravity, or DHCP mutation cannot be fenced or the approved
  convergence evidence differs;
- DHCP is authoritative but cannot be captured at one acceptable recovery
  point;
- any required exporter member/table is missing, duplicated, malformed,
  corrupt, oversized, or semantically inconsistent;
- strict validation would require importing or executing untrusted artifact
  content;
- exact import returns an incomplete/ambiguous member list, partially mutates,
  or does not complete the restart/readiness/projection/DNS proof;
- the destination is not new, empty, isolated, exact-version,
  destroy-and-recreate disposable, and provably unrelated to production;
- full service recovery is requested without the declarative environment,
  external dnsmasq file, secrets, and network prerequisites;
- a production import, source mutation, downtime, lifecycle change, secret
  creation, proxy deployment, or image rollout would occur without explicit
  approval; or
- a v5/TAR.GZ archive, another Pi-hole version, partial import, legacy field,
  fallback endpoint, or compatibility shim is requested without the user's
  explicit backward-compatibility approval.
