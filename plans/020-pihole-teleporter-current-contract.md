# Plan 020: Revalidate Pi-hole 2026.07.2 Teleporter recovery

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation; consistency and source-authority decisions
- **State**: BLOCKED — pending two explicit user policy decisions
- **Restore capability**: candidate `automatic` for the Teleporter subset only
- **Production status**: research/local planning only; every production import is forbidden
- **Fixed point**: `79b0399`

## Outcome

Replace the legacy Pi-hole adapter only after an approved consistency policy and
source-credential boundary exist. The selected implementation must use the
exact Pi-hole v6 Teleporter API, publish a strictly validated private artifact,
and restore only into fresh destroy-and-recreate disposable exact-image
destinations. It must prove phase-specific configuration, gravity relationships,
DNS behavior, FTL restart, and persistence across a second container restart in
two complete clean rounds.

The capability is never full Pi-hole service recovery. `homelab-infra` remains
responsible for the exact container declaration, environment-controlled values,
network/DNS exposure, external read-only dnsmasq file, and credentials.

Primary-source evidence and the full boundary are in
`plans/research/pihole.md`.

## Immutable identity

Pin and assert:

- image index:
  `sha256:f7d1be836e3bc608b56d82fc9904f5a831cdfbc0dc9c6d58f94e4c985c70038b`;
- linux/amd64 manifest:
  `sha256:7c96327ecfb96dbc74b0a47d073dbef7d60526e0aa87519b2a2f7a0007cb5c88`;
- Docker source `dd91b4847d97f0aac68bfefd1c108ed0627e6c68`;
- Core v6.4.3 source `f47b8ede5a8e38f9c703202d324a074dbdba4ca9`;
- Web v6.6 source `b2a4078446519c58d84f199663ca9326d5d311f0`;
- FTL/API v6.7 source `fa65a88f8cdef1013594d4de14108077954faea4`;
  and
- exact API document version 6.0/OpenAPI 3.0.2.

The tag is declaration evidence only. The exact drill uses the amd64 manifest.
Production runtime identity remains unknown until a separately approved
read-only inventory.

## Active decision gates

### Consistency policy

Exact FTL reads TOML, optional leases, and selected SQLite tables sequentially,
without one transaction spanning every member. Choose one:

1. **Quiesced export**: externally and observably prevent every configuration,
   gravity, and DHCP mutation for the complete export interval; or
2. **Accepted convergent export**: take two consecutive complete exports,
   normalize the exact restorable semantic projection, require identical hashes
   and valid relationships, and explicitly accept that this is not a
   vendor-guaranteed atomic snapshot.

Do not infer the second policy from implementation convenience. If DHCP is
authoritative or scheduled gravity/admin mutation cannot be controlled, stop.

### Source authority

Choose one:

1. approve the residual mutation authority of a distinct application password
   with `webserver.api.app_sudo=false`, after exact denied-operation evidence;
   or
2. approve a method/path-restricting proxy that exposes only authentication,
   exact version, Teleporter export, and logout calls and rejects every other
   request before Pi-hole.

Never reuse the shared administrator password or enable application sudo on a
source. No target, credential, proxy, or production grant is authorized by this
plan.

## Exact public contract after approval

Use a clean-breaking flat schema with:

- `mode`: exact `source` or `restore_destination`;
- canonical `base_url`: one origin only, with no userinfo, query, fragment, or
  ambiguous path;
- nonempty `password`, marked secret/write-only and with no default;
- explicit TLS policy with no downgrade; and
- the selected consistency/credential policy represented without aliases or
  fallback defaults.

Reject unknown fields, coercions, legacy token/login fields, unsafe origins, and
inactive-mode values. Update the frontend mock to the exact eventual capability.

Discovery and `/api/v1/plugins`, schema, TargetService persistence, public
`/test`, scheduler Run/TargetRun, RestoreService, and truthful status are all
required public seams.

## Authentication, identity, and backup

For each bounded operation:

1. POST the credential only in the `/api/auth` JSON body.
2. Require exact 200, `session.valid=true`, a bounded nonempty SID, and positive
   bounded validity.
3. Send the SID only in `X-FTL-SID`; never follow a redirect.
4. Require exact Core/Web/FTL/Docker/API version evidence from
   `/api/info/version`.
5. Apply and prove the chosen source-authority boundary.
6. Apply the chosen consistency policy and GET `/api/teleporter`.
7. Require exact 200, exact content type, bounded matching content length,
   bounded streamed EOF, and strict local validation.
8. DELETE `/api/auth` and require exact 204 before publication.

Never log or persist the origin, password, SID, configuration values, domains,
clients, leases, raw paths, or application content. `test()` returns true only
after the real non-destructive mechanism succeeds. `get_status()` may report
healthy only after the same exact identity and source-policy checks.

## Strict artifact contract

Publish one mode-0600 transactional native ZIP with a valid sidecar. Validate
before publication and again from the RestoreService-staged, descriptor-bound
artifact before any destination contact.

Require exactly the generated bounded layout:

- `etc/pihole/pihole.toml`;
- `etc/hosts`;
- `etc/pihole/gravity.db`;
- `etc/pihole/pihole-FTL.db`;
- optional `etc/pihole/dhcp.leases`; and
- bounded direct regular files under `etc/dnsmasq.d/`.

Reject extra/missing/duplicate/case-colliding members; traversal, absolute,
backslash, control, link, device, FIFO, sparse, encrypted, unsupported, or
ambiguous ZIP entries; CRC/trailer errors; and all size/count/depth/ratio/deadline
breaches. Parse TOML without evaluation. Open SQLite members immutable and
query-only, disable extension loading/trusted schema, require integrity and the
exact reduced table sets, and validate bounded types/counts plus gravity
relationships.

Bind size/SHA-256, exact component identities, normalized inventory, canonical
restorable-projection hash, validator version, and chosen consistency-policy
evidence into a secret-free sidecar. HTTP 200 and a ZIP signature are never
sufficient.

## Create-only isolated restore

Authorize restore only through RestoreService when:

- the staged artifact, sidecar, size/hash, and source provenance match;
- source and destination IDs, origins, and credentials differ;
- an explicit local-only restore flag and exact destination-origin allowlist
  pass;
- the destination is a newly created exact-manifest container with a new empty
  volume, internal-only network, no published ports, production route, shared
  namespace, production mount, or production credential; and
- supported API evidence proves the exact known-fresh semantic sentinel.

POST the unmodified ZIP with no partial import selection. Require the exact v6.7
200 JSON `files` response and complete expected processed-member set. Observe
the FTL restart, acquire a new session, recheck exact identity, export and
strictly validate the restored semantic projection, and require equality with
the artifact projection. Then prove phase markers through supported
configuration/gravity APIs and real DNS behavior.

Restart the whole destination container and repeat identity, readiness,
projection, relationship, and DNS proof. Never retry a partially changed
destination; destroy and recreate it. Any existing/shared/same/production
destination is a hard refusal.

Under these constraints, `automatic` is honest only for the native
Teleporter-restorable subset. External deployment state remains a required
recovery prerequisite.

## Vertical TDD slices after approval

1. Discovery, strict mode-aware schema/configuration, exact partial service
   boundary, and public API/TargetService seams.
2. Exact SID/version/source-authority connectivity and truthful status,
   including auth/rate-limit/redirect/TLS/response/redaction failures.
3. Chosen consistency-policy capture plus bounded streamed export, required
   logout, transactional private publication, and sidecar evidence.
4. Strict ZIP/TOML/SQLite/relationship/resource/deadline validation.
5. Restore authorization, descriptor/provenance binding, fresh-destination
   preflight, exact import response, restart/readiness, projection, DNS, and
   second-restart proof through real RestoreService.
6. Timeout/cancellation/cleanup, same-origin serialization if required, and
   failure auditing.
7. Exact two-clean-round Docker drill, documentation, reviews, and release
   gates.

Work one public tracer bullet from RED to GREEN at a time. Private parser and
process tests are reserved for unsafe archive/resource cases that cannot be
produced through the public seam.

## Exact two-clean-round drill

Each clean round must use the exact amd64 image, private internal networks,
synthetic distinct credentials and volumes, no published ports, and no
production route/mount/socket/secret. It must:

1. create a fresh source and prove exact identity, DNS readiness, fresh state,
   and the approved source boundary;
2. create phase A through supported APIs with distinct configuration, local
   DNS/CNAME, group, adlist, allow/deny domain, client relationship, and DNS
   behavior markers;
3. create scheduled artifact A under the approved consistency policy and prove
   private mode, exact sidecar, independent bytes/hash/projection, valid
   relationships, logout, no source mutation, and secret absence;
4. cumulatively create phase B, produce distinct artifact B, and rehash A to
   prove immutability and phase separation;
5. restore A and B through RestoreService into two independent fresh exact
   destinations, proving A excludes B and B contains A+B after FTL restart;
6. restart both containers and repeat exact identity, projection, API relation,
   and DNS behavior proof; and
7. remove and independently audit every labeled container, volume, network,
   session, staging file, credential, artifact copy, and temporary image.

Run the complete sequence twice from clean state: four scheduled artifacts and
four independently fresh restores. Exercise representative auth/redirect/TLS,
version, consistency-policy, archive/resource, relationship, sidecar/provenance,
same/nonfresh destination, import response, restart/projection/DNS,
cancellation, and cleanup failures.

## Verification and completion

- focused Pi-hole, API, scheduler, RestoreService, artifact, and hygiene tests;
- two complete exact clean drill rounds;
- full backend and frontend tests/lint/build;
- application mypy, changed-file Black/isort, version, diff, and secret scan;
- exact image/component health plus independent cleanup audit;
- final Standards and Spec reviews with no unresolved P0-P3 finding;
- compatibility, recovery, changelog, ledger, research, plan, and README current;
  and
- one focused milestone commit; no push or deploy.

Mark `DONE (local)` only after both policy decisions are recorded and every
item passes. Production still requires separate read-only inventory plus
explicit target, credential/proxy, TLS/network, schedule, and backup-only-run
approval. Production import remains forbidden.

## STOP conditions

Stop rather than weaken the result when either policy remains undecided; exact
identity drifts; the selected source boundary permits unapproved mutations;
quiescence/convergence cannot be proved; DHCP is authoritative but inconsistent;
artifact structure/content/relationships cannot be bounded and validated;
import response/restart/projection/DNS proof is incomplete; the destination is
not fresh, isolated, exact, and disposable; full service recovery is requested
without external deployment state; or any production import, source mutation,
downtime, lifecycle, credential, proxy, image, target, schedule, or compatibility
change would occur without explicit approval.
