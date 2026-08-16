# Cloudflare configuration export and isolated replay research

## Decision

Do not implement a broad "Cloudflare account backup" yet. The repository proves
that important state is held in Cloudflare, but it does not declare that state
completely: tunnel ingress is remotely managed in the dashboard, the only DNS
export is from 2022, and there is no declarative inventory of rulesets or Access
resources. A read-only live inventory is required to turn the intended boundary
into an exact allowlist.

The implementable product should be a **configuration export and create-only
replay plugin**, not an account clone. Its first exact boundary should cover the
selected zone's customer-managed DNS records, the two remotely managed tunnel
configurations, and any private tunnel routes discovered by inventory. Rulesets
and Cloudflare Access applications/policies may be added only after inventory
proves which resource kinds and cross-resource references exist.

Backup can use a dedicated read-only API token and needs no downtime. Restore
must use a different write token scoped only to a disposable destination
account/zone. Production restore remains forbidden. Local HTTP mocks can prove
our request, normalization, validation, and failure behavior, but Cloudflare
does not provide a local control-plane emulator; a production-ready restore
claim therefore requires a real disposable Cloudflare account and zone.

This research made no Cloudflare API request and did not contact, back up, or
modify production.

## Exact repository evidence

| Evidence | Conclusion |
| --- | --- |
| Docker-host [`cloudflared.yaml`](../../../homelab-infra/docker.compose/system/cloudflared/cloudflared.yaml) and DMZ [`cloudflared.yaml`](../../../homelab-infra/docker.compose/dmz/cloudflared/cloudflared.yaml) both run `cloudflare/cloudflared:2026.7.3` with a tunnel token and no local tunnel configuration file. | The connectors are stateless consumers of remotely managed configuration. Their images and tokens are deployment concerns, not backup artifacts. |
| The DMZ guide calls out a distinct Docker-host tunnel and DMZ tunnel, and instructs operators to edit Cloudflare Zero Trust ingress in the dashboard: [`docker.compose/dmz/AGENTS.md`](../../../homelab-infra/docker.compose/dmz/AGENTS.md). | Two logical tunnel configurations are expected, but their exact live IDs, names, ingress rules, and status are absent from source control. |
| SFTPGo's runbook describes a Docker-host public-hostname route: [`docker.compose/misc/sftpgo/README.md`](../../../homelab-infra/docker.compose/misc/sftpgo/README.md). The Speakr runbook records a per-ingress `disableChunkedEncoding` dashboard setting: [`doc/docker/speakr-mobile-upload.md`](../../../homelab-infra/doc/docker/speakr-mobile-upload.md). | Tunnel ingress contains operationally significant settings that would be lost if only the connector containers were recreated. |
| [`dns-hollinger.asia-export.txt`](../../../homelab-infra/doc/cloudflare/dns-hollinger.asia-export.txt) is a 15-record BIND export dated 2022-06-06. | It is useful historical evidence, not a current authoritative backup. Do not seed implementation fixtures with its real values. |
| Docker-host and DMZ fail2ban actions dynamically create and remove account-level IP Access Rules: [`files/docker/.../cloudflare.conf`](../../../homelab-infra/files/docker/docker-configs/fail2ban/actions.d/cloudflare.conf), [`files/dmz/.../cloudflare.conf`](../../../homelab-infra/files/dmz/fail2ban/actions.d/cloudflare.conf). | Current bans are derived, short-lived security runtime state. Exclude them from disaster-recovery replay; fail2ban should repopulate them from new events. |
| No Cloudflare Access application, ruleset, zone-setting, or Terraform-style Cloudflare resource is declared in `homelab-infra`. | Absence in the repository is not proof that these resources are absent from the account. Read-only API inventory is mandatory before selecting them. |

The repository also contains local secret files used by the connectors and
fail2ban. Their values were deliberately not copied into this note. They are not
an acceptable source for the plugin credential and must never be included in an
artifact.

## Authoritative state boundary

### Selected core boundary

| State | Disposition |
| --- | --- |
| Customer-managed DNS records for the selected zone | Include all pages and all fields accepted by the corresponding create operation, including type-specific `data`, TTL, proxy state, comments, tags, and settings. Exclude response-only IDs and timestamps. |
| Remotely managed `cloudflared` tunnels | Include logical tunnel name, `config_src`, and the complete configuration returned by `GET .../configurations`, including ordered ingress, terminal catch-all, `originRequest`, and `warp-routing`. |
| Private tunnel routes | Include only active `cfd_tunnel` routes, normalized to tunnel name plus virtual-network name and portable route fields. An empty collection is meaningful. |
| Tunnel connector image/runtime/connections | Exclude. Compose/Ansible recreates connectors; connection IDs, colos, status, origin IPs, and timestamps are volatile. |
| Tunnel and API tokens | Exclude. Never call the tunnel-token endpoint. Cloudflare states that anyone holding a remotely managed tunnel token can run that tunnel. Tokens must be regenerated or escrowed outside Homelab Backup. |
| Account-level IP Access Rules created by fail2ban | Exclude as derived runtime state. Any static human-managed rule discovered in the same API collection is an ambiguity and a STOP condition until static and dynamic ownership can be distinguished. |
| Zone membership, nameserver delegation, subscription, registrar state, DNSSEC keys | Exclude. The destination zone must be provisioned and delegated separately. |

Cloudflare's official API exposes list/create/update/delete operations for DNS
records and a native BIND export/import endpoint. The BIND form is useful as a
human recovery aid, but normalized JSON must remain canonical because it gives
the plugin strict type-aware validation, stable comparison, and create-only
replay without relying on import defaults. Cloudflare now carries proxy state,
comments, tags, and CNAME-flattening hints in exported `cf-` tags, so include a
BIND export as an optional second member generated by the official endpoint,
not as the sole source of truth:
[DNS record API](https://developers.cloudflare.com/api/resources/dns/subresources/records/),
[import/export behavior](https://developers.cloudflare.com/dns/manage-dns-records/how-to/import-and-export/).

Cloudflare documents remotely managed configurations as Cloudflare-held state,
retrievable by
`GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations`; the returned
configuration includes ordered public-hostname ingress and origin parameters
such as `disableChunkedEncoding`:
[configuration API](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/configurations/methods/get/),
[remote versus local management](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/).

### Conditional extensions after inventory

- **Rulesets:** list both account and zone rulesets, then fetch each latest
  ruleset because list responses omit rules. Include only customer-owned
  resources and selected supported phases. Managed/inherited rulesets are
  provider state. Rules can reference managed rulesets, account lists, products,
  and plan-specific features, so an unknown reference blocks replay rather than
  being copied blindly. Cloudflare's list endpoint returns rulesets across
  phases and documents phase-specific read permissions:
  [list and view rulesets](https://developers.cloudflare.com/ruleset-engine/rulesets-api/view/).
- **Access:** list account Access applications, per-application policies, and
  reusable policies. Include them only if all referenced identity providers,
  groups, lists, service-token identities, and audience references can be
  mapped by stable logical names in the destination. API-generated IDs and
  application audience tags must be rewritten after creation:
  [applications](https://developers.cloudflare.com/api/resources/zero_trust/subresources/access/subresources/applications/),
  [application policies](https://developers.cloudflare.com/api/resources/zero_trust/subresources/access/subresources/applications/subresources/policies/),
  [reusable policies](https://developers.cloudflare.com/api/resources/zero_trust/subresources/access/subresources/policies/).
- **Zone settings and other products:** exclude until explicitly selected.
  Cloudflare's endpoint for listing every zone setting is deprecated, while the
  supported endpoint reads one known setting at a time. A generic scraper
  cannot claim completeness without a fixed setting allowlist:
  [zone setting API](https://developers.cloudflare.com/api/resources/zones/subresources/settings/methods/get/).

## Least-privilege backup access

Use `https://api.cloudflare.com/client/v4/` as a fixed origin, Bearer
authentication, bounded timeouts, complete endpoint-specific pagination, and
the documented rate-limit headers. Never accept an arbitrary API base URL in
production configuration. Cloudflare explicitly recommends API tokens instead
of the global API key and warns not to store token secrets in plaintext:
[API calling convention](https://developers.cloudflare.com/fundamentals/api/how-to/make-api-calls/),
[rate limits](https://developers.cloudflare.com/fundamentals/api/reference/limits/).

Create a new account token for Homelab Backup. Do not reuse either connector
token or the fail2ban write token. Scope it to the one account and selected
zone, with only:

- `Zone Zone Read` to resolve and verify the exact zone;
- `DNS Read` for record listing and native export; and
- `Cloudflare Tunnel Read` for tunnel listing, remote configuration,
  and private-route listing. Cloudflare accepts Tunnel Read for all three
  read paths: [tunnel list](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/methods/list/),
  [configuration get](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/configurations/methods/get/),
  [route list](https://developers.cloudflare.com/api/resources/zero_trust/subresources/networks/subresources/routes/methods/list/).

If inventory selects optional surfaces, add `Access: Apps and Policies Read`
and only the exact ruleset read permissions required by the observed phases.
Do not grant a blanket write permission merely because Cloudflare documents it
as an alternative accepted permission for a GET endpoint. The plugin's
`test()` must perform the actual required GETs and report a missing scope or
partial pagination distinctly; token validity alone does not prove coverage.

## Proposed artifact contract

Publish one canonical, deterministic JSON document through the repository's
atomic artifact helper. It should contain:

- schema and exporter versions, capture timestamp, Cloudflare API v4 origin,
  source account/zone provenance, and declared scope names;
- normalized DNS records plus the optional native BIND export;
- normalized logical tunnel descriptors and full remote configurations;
- normalized active private routes keyed by tunnel/virtual-network names;
- explicitly selected conditional collections, including empty collections;
- per-collection counts and a canonical content hash; and
- no raw API envelope, credentials, connector state, or response-only audit
  fields.

The artifact is sensitive even without credentials. DNS TXT records may carry
verification material; tunnel configs disclose private origins and routing;
Access policy may contain identities and authorization logic; rules disclose
security posture. Keep artifact and sidecar private, never log response bodies,
and place only schema version, collection counts, byte size, and SHA-256 in the
sidecar. Use positive field allowlists rather than trying to redact arbitrary
raw Cloudflare responses after collection.

Validation before publication must require:

1. Every API envelope has `success: true`, no errors, the expected result type,
   and complete pagination with bounded page/item/byte limits.
2. Exactly one expected active zone; exact account binding; no deleted tunnel
   or route; and only `config_src == "cloudflare"` selected for configuration
   export.
3. Unique normalized DNS identities, valid in-zone names and type-specific
   payloads, bounded strings, and only fields supported by the current create
   schema.
4. Unique tunnel names, nonempty ordered ingress, a valid terminal catch-all,
   supported service schemes, and all route references resolving to a selected
   tunnel and virtual network.
5. Conditional rules/Access references form a closed graph or are explicitly
   listed as external restore prerequisites. Unknown kinds, phases, actions,
   generated IDs without mappings, or secret-like response fields fail closed.
6. Deterministic serialization, nonzero bytes, private mode, a matching
   sidecar, and reread hash/size equality before success is returned.

A capture is not consistent if independent resources change while their pages
are read. Read a compact fingerprint (record count/modified markers, tunnel
configuration versions, and selected ruleset versions) before and after the
bounded collection; retry from scratch on change. If an endpoint has no useful
revision marker, re-fetch and compare its normalized value. This is optimistic
read consistency, not a Cloudflare-wide transaction.

## Restore contract

Restore is create-only and isolated. It must require the existing global local
restore opt-in plus an additional Cloudflare destination allowlist, a separate
destination write token, and destination account/zone IDs that differ from the
artifact's source IDs. Never expose a production Cloudflare restore target in
the normal target form.

The destination token needs `DNS Write` and `Cloudflare Tunnel Write` for the
core boundary, scoped only to the disposable resources. Add
`Access: Apps and Policies Write` and the exact phase-specific ruleset write
permissions only when those conditional collections were selected. Do not
combine source read authority and destination write authority in one token.

Provision outside the plugin:

- a disposable Cloudflare account and active empty zone;
- the required plan/features and equivalent Access identity dependencies;
- fresh remotely managed tunnels with new connector tokens already delivered
  out of band; and
- a dedicated destination token scoped only to those disposable resources.

Pre-created tunnels are intentional. Creating a tunnel requires a tunnel
secret, and its resulting token is an operational credential. The plugin must
neither back up the old token nor generate a new token into job output or logs:
[create tunnel API](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/methods/create/),
[tunnel-token security](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/remote-tunnel-permissions/).

Before the first write, require no customer DNS records, no active private
routes, no customer-owned rulesets/Access apps in selected optional scopes, and
an exact one-to-one mapping from artifact tunnel names to destination tunnels
whose configuration is only the agreed inert `http_status:404` placeholder.
Provider-managed immutable resources are allowed only through an exact
versioned allowlist.

Replay dependency-first: reusable Access dependencies and applications (if
selected), customer rulesets (if selected), tunnel configurations, private
routes, then DNS last. Rewrite source IDs/audience tags to destination IDs;
never preserve them by accident. Use create/PUT operations only and refuse to
delete or overwrite a nonfresh destination. Cloudflare supports `PUT` of a
remote tunnel configuration and DNS batch changes, but DNS propagation remains
non-atomic even when the batch database transaction succeeds:
[put tunnel configuration](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/configurations/methods/update/),
[DNS batch semantics](https://developers.cloudflare.com/dns/manage-dns-records/how-to/batch-record-changes/).

After replay, re-export every selected destination surface, normalize it with
the generated ID mapping, and require semantic equality with the artifact.
Record every created resource in a restore journal. If a request fails, report
`partial` with the journal; do not attempt an automatic destructive rollback.
The disposable destination is discarded after the drill.

## Proof plan

Local tests can use `httpx.MockTransport` to prove pagination, ordering,
normalization, retries, redaction, immutable source credentials, same-target
rejection, freshness checks, ID rewrites, partial journals, cancellation, and
every negative validation path. Those tests are necessary but not sufficient:
`cloudflared` is a connector, not a local implementation of Cloudflare's API or
control plane.

The exact drill requires explicit authority for writes to a disposable
Cloudflare account and zone, never production:

1. Create isolated source fixtures for DNS and two remote tunnel configs with
   synthetic hostnames/origins; optionally add only the exact rules/Access
   resource kinds selected after inventory.
2. Take artifact A, restore to fresh isolated destination A, and require full
   API re-export equality.
3. Mutate the isolated source with a second marker/configuration, take artifact
   B, restore to fresh destination B, and prove independent hashes and semantic
   differences.
4. Exercise invalid token/scope, incomplete pagination, changing-source,
   malformed record/config, missing tunnel mapping, nonfresh destination,
   unsupported rule/reference, rate limit, timeout, and partial replay cases.
5. Delete the disposable resources using a separately authorized cleanup
   procedure after evidence is captured. Cleanup is not plugin restore logic.

No connector needs to route production traffic during this drill. Use reserved
example hostnames and documentation/test-network origin addresses so accidental
publication cannot reach the homelab.

## STOP conditions

Stop and report without publishing a backup or performing a restore when:

- read-only live inventory has not established the exact zones, logical
  tunnels, routes, ruleset phases, and Access resource kinds in scope;
- any required collection is forbidden, truncated, changes during capture, or
  cannot be proved complete with the granted read scopes;
- a static account IP rule cannot be distinguished from fail2ban-derived bans;
- a tunnel is locally managed, deleted, duplicated by name, lacks a retrievable
  configuration, or contains an unsupported ingress/service field;
- rules or Access policies reference an unexported list, identity provider,
  group, service token, audience, secret, product, or plan-specific resource;
- artifact construction would require calling a token/secret endpoint or
  storing an API token, tunnel token, client secret, cookie, or authorization
  header;
- the restore target is the source account/zone, is not explicitly disposable
  and allowlisted, is nonempty, lacks equivalent plan/features, or its tunnel
  mapping is not exact;
- a replay path requires update/delete of existing customer state, automatic
  rollback, production mutation, or production restore;
- only mocked tests are available but the capability is being represented as
  exact-restored; or
- Cloudflare changes an endpoint/schema/permission such that the versioned
  allowlists no longer match.

## What is decidable now

The core API shape, secret boundary, artifact model, non-destructive backup
behavior, and isolated create-only restore design are decidable. Implementation
is **not fully unblocked**: one production read-only inventory is needed to fix
the selected surfaces and permissions, and one disposable Cloudflare
account/zone is needed to prove restore semantics. Until both gates are met,
classify Cloudflare as researched/blocked rather than a completed plugin.
