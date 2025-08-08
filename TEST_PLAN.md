# Test Plan (v0)

## Unit
- Schema validation
- Plugin artifact path builder
- Metrics counters
- Cron parser

## Integration
- Pi-hole mock → artifact present (non-zero)
- Postgres local container → pg_dump produces file

## E2E (later)
- Dashboard shows last run and artifact link

## CI
- GitHub Actions: lint + test on push/PR
