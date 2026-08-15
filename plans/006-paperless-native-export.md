# Plan 006: Paperless-ngx 2.20.15 native export and isolated restore

## Status

- **Priority**: P0
- **Effort**: L
- **Risk**: HIGH
- **Depends on**: Plan 001 foundation and explicit production execution-path
  approval
- **State**: BLOCKED
- **Production status**: BLOCKED; production restore remains forbidden
- **Researched at**: Paperless-ngx 2.20.15 / upstream commit
  `05e48b23166df7c7afe6f329b460b0511a89496c`, 2026-08-15

## Outcome

Add one `paperless` plugin that triggers the exact native exporter inside the
declared Paperless container, publishes a validated version-pinned ZIP and
sidecar, and restores only into a fresh disposable exact-version stack through
the matching native importer.

The authoritative boundary and rejected composite design are documented in
`plans/research/paperless-ngx.md`.

## Public contract

Keep configuration flat and non-shell-like. Pin the declared Paperless
container identity and exact image/version; do not accept an arbitrary command.
`test()` must non-destructively prove the exact image, native exporter/importer
availability, source readiness, and permitted execution/archive operations.

Declare automatic restore only if the plugin enforces an explicitly labeled
fresh destination and proves application readiness plus restored content.

## Test-first slices

1. Discovery, flat schema, exact container/image validation, and secret-safe
   connectivity errors.
2. Fixed exporter invocation, unique staging name, bounded lifecycle, and
   overlap refusal.
3. Streaming archive transfer into a transactional artifact without
   artifact-sized memory growth.
4. Strict ZIP, metadata, manifest, member-path, size, referenced-file, version,
   private-mode, and sidecar validation.
5. Timeout, cancellation, exporter failure, archive-transfer failure, and
   staging cleanup with child work proven stopped.
6. Fresh disposable restore preconditions, exact-version importer invocation,
   and non-empty destination refusal.
7. Restore timeout/cancellation/partial-failure rollback before readiness.
8. Post-import health, cardinality, representative record, original checksum,
   archive/thumbnail, index, and sanity-check evidence.
9. Real API discovery/schema/test behavior.
10. Two consecutive exact-topology local backup-to-fresh-restore drills.

## Done criteria

- [ ] The production execution path is explicitly approved.
- [ ] All ten test-first slices pass.
- [ ] Two exact-version backup-to-fresh-restore drills pass.
- [ ] Full backend/frontend checks pass.
- [ ] Standards/spec review has no unresolved P0/P1 findings.
- [ ] Compatibility, recovery, changelog, and infrastructure requirements are
      documented.
- [ ] The milestone is committed independently with this plan marked DONE.

## STOP conditions

Stop before implementation without explicit approval for the production
execution privilege. Stop before production changes, any restore outside the
disposable dev stack, cross-version import, non-empty destination import,
arbitrary command execution, artifact encryption, or an independently timed
PostgreSQL/media composite.
