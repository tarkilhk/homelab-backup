# Plan 010: pfSense native encrypted configuration export and isolated restore proof

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation
- **State**: BLOCKED before implementation
- **Production status**: STOP-gated; no production call, target, credential,
  export, write, or restore is authorized by this plan
- **Exact local-drill target**: pfSense CE 2.8.1, configuration revision 24.0,
  pfREST v2.8.3 / FreeBSD package `2.8_3`
- **Research basis**: `plans/research/pfsense.md`

The implementation blocker is a deliberate contract conflict. The safest honest plugin
capability is `manual`: the plugin exports and validates a native encrypted
configuration but exposes no restore action. `ADDING_PLUGINS.md` says new
backup-only/manual plugins are outside the completion contract. Do not disguise
a validation-only `restore()` as `partial`. Before registering or merging the
plugin, obtain an explicit project decision to either admit this manual
exception or approve a separate restore design which is proven unable to target
production. The user's current rule also requires restore whenever backup is
built, so implementation must not begin while that decision is pending.

## Outcome

If the blockers above are explicitly resolved, add one exact-version `pfsense`
plugin that authenticates to webConfigurator,
uses the CSRF-protected native Diagnostics > Backup & Restore form to download
one password-encrypted full `config.xml`, strictly validates the encrypted and
decrypted formats without persisting plaintext, and publishes the original
encrypted response and normal sidecar transactionally.

The export includes package configuration and SSH host keys. It deliberately
excludes RRD, volatile Captive Portal/voucher/DHCP lease data, package binaries,
the pfSense OS, runtime state/logs, and host changes such as
`/usr/local/sbin/rah_scripts`, `/etc/rc.newwanip`, and `/etc/rah`. Backup is
online and must not stop, reload, restart, reboot, or write the firewall.

Prove recovery twice by restoring synthetic data through the official GUI into
a newly created, deny-production pfSense VM. This drill is outside the plugin's
runtime restore path and never uses a production artifact or secret.

## Fixed public contract

Create the conventional package:

```text
backend/app/plugins/pfsense/
├── __init__.py
├── plugin.py
└── schema.json
```

`__init__.py` re-exports exactly `PfSensePlugin`. The stable plugin key is
`pfsense`, the artifact prefix is `pfsense-config`, the suffix remains `.xml`
to preserve the native shape, and `restore_capability = "manual"` unless the
contract blocker above is resolved by a newly approved design.

Keep the schema flat, set `additionalProperties: false`, and require exactly:

- `base_url`: one HTTPS origin for webConfigurator and pfREST; no userinfo,
  query, fragment, non-root path, IP ambiguity, or off-origin redirect;
- `username` and `password`: dedicated local webConfigurator account;
- `api_key`: separate pfREST key for read-only inventory;
- `encryption_password`: distinct password for the native artifact;
- `expected_hostname`, `expected_domain`, `expected_pfsense_version`,
  `expected_buildtime`, and `expected_config_revision`;
- `accept_combined_backup_restore_privilege`: literal `true`, documenting the
  accepted residual authority of `page-diagnostics-backup-restore`;
- optional `ca_bundle_path`: canonical readable regular non-symlink CA bundle.

Give no credential a default. Use `format: "password"` and `writeOnly: true`
for the three secrets. If needed, teach the schema-driven target form to render
that format as a password input and cover it with a frontend test. This masks
screen entry only; it does not solve the repository's existing target-config
at-rest storage boundary, which must not be silently redesigned in this
milestone. Do not add `verify_tls`, legacy field aliases, alternate endpoints,
SSH credentials, command strings, compatibility fallbacks, or user-adjustable
security/size limits.

Exact local fixtures use `https://pfsense-source.test.invalid:10443`, synthetic
credentials, CE `2.8.1-RELEASE`, the matching build time captured from the lab
VM, config revision `24.0`, and pfREST installed version `2.8_3`. Production
values remain unknown and may not be guessed from the repository pin.

## Read-only protocol and status contract

All clients use finite connect/read/write/pool deadlines and
`follow_redirects=False`. Credentials, API keys, cookies, CSRF values, artifact
passwords, full URLs, native filenames, host/domain values, XML excerpts, and
vendor response bodies are absent from logs and exception text.

`validate_config()` performs deterministic shape checks only. It requires four
independent non-empty secrets, exact expected values, HTTPS, safe optional CA
path, and the combined-privilege acknowledgement. It rejects unknown keys and
does not coerce absent values to strings.

`test()` performs only this sequence:

1. `GET /api/v2/system/version` with `X-API-Key` and require exact expected
   version/build values.
2. `GET /api/v2/system/packages` with the same key, select RESTAPI in memory,
   and require installed version `2.8_3`.
3. Start a fresh cookie jar, fetch the exact login form and CSRF value, submit
   only the native login fields, then `GET /diag_backup.php` and require the
   exact full-backup form, its CSRF value, and the expected option names.
4. Close the session. Do not submit `download` from `test()`.

The pfREST user has only `api-v2-system-version-get` and
`api-v2-system-packages-get`. The webConfigurator user is neither `admin` nor
in `admins`, has no shell/all-pages/command privilege, and has only
`page-diagnostics-backup-restore` plus `user-config-readonly`. Code cannot prove
those assignments from the page alone; they remain a provisioning and
production-acceptance gate.

`get_status()` repeats only the two inventory GETs and returns `ok` only after
both exact checks pass. It returns `unknown` for an unobservable endpoint and
must not manufacture health from configuration values.

`backup()` repeats the inventory preflight, creates a new webConfigurator
session, parses a fresh CSRF token, and makes one export POST containing exactly:

- `download` set;
- empty `backuparea`;
- `donotbackuprrd` and `backupssh` set;
- `encrypt` set;
- matching non-empty `encrypt_password` and `encrypt_password_confirm`;
- the exact current CSRF field.

It omits `nopackages` and `backupdata`. Tests assert the complete request-key
set and prove that no restore, apply, reinstall, clear, history, command,
service-control, or pfREST mutation request can be emitted. A changed form or
CSRF flow fails closed; there is no legacy `Submit=download`, unencrypted, SSH,
or command-prompt fallback.

## Artifact and secret contract

Stream the encrypted response only to the temporary path yielded by
`create_backup_artifact()`, created with mode `0600`. Cap the encrypted response
at 64 MiB and the decrypted XML at 48 MiB. Abort the stream on overflow,
timeout, cancellation, HTML/login content, off-origin redirect, unexpected
content disposition, or wrong media shape, and remove the partial file.

Before the artifact helper exits, require all of the following:

1. HTTP success and the native
   `config-<hostname>.<domain>-<14-digit timestamp>.xml` attachment filename.
2. Exactly one `---- BEGIN config.xml ----` and one
   `---- END config.xml ----`, bounded canonical base64, no unexpected wrapper
   content, and decoded prefix `Salted__` with an eight-byte salt.
3. In-memory PBKDF2-HMAC-SHA256 derivation at exactly 500,000 iterations,
   AES-256-CBC decryption, and strict PKCS#7 unpadding through the existing
   `cryptography` dependency. Never pass the password through argv/environment,
   invoke OpenSSL, or create a plaintext temporary file.
4. Reject `DOCTYPE` and `ENTITY` before parsing. Parse with a non-networked XML
   parser; require root `<pfsense>`, numeric schema revision and revision time,
   exact hostname/domain/config revision, required full-system sections,
   `installedpackages` with RESTAPI configuration, and `sshdata`.
5. Reject `rrddata`, volatile `*data/xmldatafile` payloads, missing package/SSH
   data, duplicate critical sections, malformed encoding, and schema versions
   newer than or different from the one explicitly configured.
6. Hash and count the still-encrypted bytes independently, then let
   `create_backup_artifact()` atomically publish the non-empty regular file and
   standard sidecar. Return `artifact_path` only after both exist.

Keep the current generic sidecar contract; do not add a second ad hoc sidecar.
The run ledger independently records size/SHA-256. Extra non-secret evidence
(exact lab release/build/config revision, export option booleans, and expected
versus observed checks) belongs in the redacted drill report, never in the
artifact or logs. Best-effort overwrite mutable plaintext/password/session
buffers before releasing them; tests primarily prove no plaintext filesystem,
process, log, exception, or retained-object copy exists.

`restore()` must make no network or filesystem mutation and raise a concise
manual-restore error if called directly. `RestoreService` already rejects a
manual capability before dispatch; cover both boundaries. Do not return
`partial` or `success` without an approved implementation which actually
restores and verifies a fresh destination.

## Test-first vertical slices

Implement one failing observable test, the minimum code to pass it, then repeat:

1. Discovery, flat strict schema, password rendering, exact CE 2.8.1 constants,
   manual capability, configuration rejection, and secret-safe API errors.
2. Read-only pfREST inventory using `httpx.MockTransport`: exact
   version/build/package success plus DNS/TCP/TLS, 401/403, malformed JSON,
   missing/duplicate RESTAPI, wrong package version, unsupported release,
   timeout, cancellation, and off-origin redirect failures.
3. Exact login and backup-form parsing from synthetic CE 2.8.1 HTML fixtures:
   cookie/CSRF rotation, login rejection, missing privilege/form/options,
   same-origin enforcement, and proof that `test()` never exports.
4. Independent known-answer encryption fixtures: native wrapper/base64/salt,
   PBKDF2 parameters, AES-CBC, padding, wrong password, truncation, duplicate
   markers, garbage, and encrypted/plaintext size ceilings.
5. Hardened full-XML validation using generated synthetic configuration:
   required boundary, exact identity/revision, package settings/key hashes,
   SSH keys, exclusion checks, DTD/entity/duplicate-element attacks, malformed
   XML, wrong version/host, and missing/extra state.
6. Exact export request and streaming publication: only approved POST keys,
   fresh CSRF, restrictive mode, unique native artifacts, valid standard
   sidecars, independent size/hash, and no artifact-sized plaintext disk copy.
7. Failure atomicity and redaction: HTTP/body/header errors, stream failure,
   overflow, bad encryption/XML, sidecar failure, timeout, repeated
   cancellation, and concurrent runs leave no temporary/final/plaintext file or
   child work and reveal none of a seeded secret corpus.
8. Manual restore boundary and honest status: direct `restore()` and
   `RestoreService` refuse before destination I/O; observed inventory can report
   `ok`, while unobserved/failed inventory reports `unknown` or a documented
   redacted error.
9. Real plugin list/schema/test API behavior with `ASGITransport`, including
   schema discovery, required fields, masked frontend secrets, and exact
   user-facing exception mapping.
10. Two consecutive exact-version synthetic backup-to-fresh-restore drills with
    distinct artifacts and independent recovery evidence as specified below.

No default unit/API test resolves or contacts a real hostname. All protocol
tests use `MockTransport`; the VM drill is separately selected and requires an
explicit lab-only acknowledgement.

## Create-only isolated two-VM drill

The drill uses only synthetic state and at most one source and one target VM
online. Record the official CE 2.8.1 installer filename and SHA-256, the pfREST
v2.8.3 release-asset SHA-256, virtual NIC count/order/model, configuration
revision, and build time before running.

1. Create a source VM on a deny-production virtual switch with documentation
   addresses/domains and disposable credentials. There is no default route to
   production. Hypervisor rules deny production RFC1918/ULA ranges, DNS, CARP,
   VPN, ACME, SMTP, syslog, SSH/SCP, and `pfsense.tarkilnetwork`; capture denied
   traffic as evidence.
2. Install CE 2.8.1, exact pfREST `2.8_3`, and representative packages. Seed
   uniquely labeled synthetic interfaces/VLANs, aliases, firewall/NAT, DHCP
   mapping, Unbound override, HAProxy, disabled VPN, ACME/certificates, local
   users/privileges, pfREST settings/key hash, and SSH host keys. Do not copy any
   production configuration or secret.
3. Run plugin `test()` and `backup()` against the source. Independently verify
   artifact A's path, mode, non-zero size, SHA-256, sidecar, native decryption,
   expected synthetic markers, and intentional exclusions.
4. Power off the source. Create a pristine target VM with identical NIC
   count/order/model on a separate deny-production switch and keep console
   access. Install the same CE release. Through the official GUI only, manually
   upload/decrypt the artifact and perform a full restore; the target may reboot.
5. Reinstall exact package binaries from checksum-pinned controlled sources.
   Prove boot/readiness, config revision, every synthetic marker and SSH
   fingerprint, restored package settings/key behavior, and absence of
   RRD/leases/runtime state and host-only Ansible tweaks. Packet capture must
   show zero production reachability.
6. Destroy the first target, power the isolated source back on, create and
   validate distinct artifact B, then power the source off again. Create a new
   pristine target and repeat the restore/validation steps. Compare A-versus-B
   evidence. At no time may the source and restored target be online together
   or share a live segment or duplicate DHCP/IP identity.
7. Retain only a redacted checklist with release/build/revision, hashes, sizes,
   readiness/content assertions, package reconciliation, and network-isolation
   evidence. Destroy disposable VMs and artifacts under the lab cleanup policy.

The drill driver may validate preconditions, run export/inspection, and collect
read-only post-restore evidence. It must never automate the restore upload,
apply, reboot, package mutation, or VM provisioning against an arbitrary URL.

## Expected implementation files

- `backend/app/plugins/pfsense/{__init__.py,plugin.py,schema.json}`
- `backend/tests/plugins/test_pfsense_plugin.py`
- `backend/tests/test_api/test_plugins_api.py` only for public discovery/schema
  assertions not already generic
- frontend target-form code/tests only if password schema rendering is missing
- one opt-in lab drill/evidence helper under `backend/scripts/` or
  `backend/tests/integration/`, with an unmistakable lab-only name and guard
- `docs/PLUGIN_COMPATIBILITY.md`, recovery documentation, and changelog after
  the merge/capability decision is resolved

Do not edit the coverage ledger as part of this milestone. Do not add a
production target, schedule, credential, firewall rule, user, API key, CA, or
infrastructure automation.

## Checks and done criteria

Run from the repository virtual environment:

```bash
cd backend
.venv/bin/pytest -q tests/plugins/test_pfsense_plugin.py
.venv/bin/pytest -q tests/test_api/test_plugins_api.py
.venv/bin/pytest -q
.venv/bin/mypy app tests
.venv/bin/black --check app tests
.venv/bin/isort --check-only app tests
```

If password rendering changes the frontend:

```bash
cd frontend
npm test -- --run
npm run lint
npm run build
```

Local engineering is complete only when slices 1-9 pass, no default test can
reach a real network, secret-redaction fixtures pass, and standards/spec review
has no unresolved P0/P1 finding. Local recovery proof is complete only when two
fresh-target drills pass with independent size/hash/content/readiness/isolation
evidence. Mark the milestone DONE and register/release the plugin only after the
manual-capability contract decision is recorded. Production remains STOP-gated
even after local completion.

## Production STOP gates

No production call is authorized by this plan. The first future production
action, if separately approved, is limited to collecting read-only inventory;
it must not log in with the combined backup/restore account or export anything.
After that inventory is reviewed, stop again before a connectivity test or
export and obtain all of the following as explicit evidence:

- a read-only live inventory recording exact CE/Plus edition, release, patch,
  build time, architecture, configuration revision, and installed RESTAPI
  `2.8_3`; it must match the implementation's pinned source/form/crypto contract
  and an available authorized lab image;
- explicit acceptance of the residual destructive restore authority carried by
  `page-diagnostics-backup-restore`, despite `user-config-readonly`;
- a dedicated local non-admin/no-shell webConfigurator user with only those two
  page privileges, and a separate API key user with only the two GET privileges;
- a verified HTTPS chain/hostname (or explicit trusted CA bundle), same-origin
  routing, source-IP management-interface firewall restriction, pfREST access
  list, and login protection;
- an artifact encryption password distinct from both access credentials and an
  approved escrow/recovery process.

If the live system is not the exact locally proven CE 2.8.1 contract, stop and
research its matching official source. Do not add compatibility behavior or
assume CE/Plus equivalence without explicit approval.

## Absolute STOP conditions

Stop without fallback for any production contact before the gates above; any
production restore/write/reboot/service control; any reuse of the deployed
broad HAProxy/exporter key; disabled TLS verification; off-origin redirect;
unexpected form/CSRF/version/package/config revision; privilege expansion;
legacy alias, SSH/root, command-prompt, or unencrypted fallback; plaintext XML
or secrets on disk/in logs/errors; malformed/oversized/incomplete artifact;
proposed backup downtime; or a lab target which is not fresh, disposable,
console-accessible, checksum-pinned, and deny-production isolated.

Also stop before calling this plugin complete while it remains `manual` unless
the project explicitly approves that exception to `ADDING_PLUGINS.md`. A
`partial` label without a real safe destination-changing restore is forbidden.
