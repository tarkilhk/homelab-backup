/**
 * Default MSW request handlers for tests.
 * These provide baseline mock responses that can be overridden in individual tests.
 */
import { http, HttpResponse } from 'msw'

// Default timestamp for consistent test data
const nowIso = new Date().toISOString()

// Default mock data
const defaultBackups = [
  {
    artifact_path: '/backups/pihole/2025-01-15/pihole-backup-20250115T120000.zip',
    target_slug: 'pihole',
    date: '2025-01-15',
    plugin_name: 'pihole',
    file_size: 1024000,
    modified_at: nowIso,
    metadata_source: 'sidecar',
  },
  {
    artifact_path: '/backups/postgresql/2025-01-16/postgresql-dump-20250116T130000.sql',
    target_slug: 'postgresql',
    date: '2025-01-16',
    plugin_name: 'postgresql',
    file_size: 2048000,
    modified_at: nowIso,
    metadata_source: 'inferred',
  },
  {
    artifact_path: '/backups/unknown/2025-01-17/unknown-backup-20250117T140000.tar.gz',
    target_slug: 'unknown',
    date: '2025-01-17',
    plugin_name: null,
    file_size: 512000,
    modified_at: nowIso,
    metadata_source: 'inferred',
  },
]

const defaultTargets = [
  { id: 1, name: 'Primary Pihole', slug: 'pihole', plugin_name: 'pihole', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
  { id: 2, name: 'Secondary Pihole', slug: 'pihole-secondary', plugin_name: 'pihole', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
  { id: 3, name: 'PostgreSQL DB', slug: 'postgresql', plugin_name: 'postgresql', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
]

const defaultPlugins = [
  { key: 'pihole', name: 'Pi-hole', version: '1.0.0' },
  { key: 'postgresql', name: 'PostgreSQL', version: '1.0.0' },
  { key: 'mysql', name: 'MySQL', version: '1.0.0' },
]

export const handlers = [
  // List available backups
  http.get('/api/v1/backups/from-disk', () => {
    return HttpResponse.json(defaultBackups)
  }),

  // List targets
  http.get('/api/v1/targets/', () => {
    return HttpResponse.json(defaultTargets)
  }),

  // List plugins
  http.get('/api/v1/plugins', () => {
    return HttpResponse.json(defaultPlugins)
  }),

  // Restore endpoint
  http.post('/api/v1/restores/', async ({ request }) => {
    const body = await request.json() as { artifact_path?: string; destination_target_id?: number }
    if (body.artifact_path && body.destination_target_id) {
      return HttpResponse.json({
        id: 99,
        job_id: null,
        status: 'success',
        operation: 'restore',
        started_at: nowIso,
        finished_at: nowIso,
        job: null,
        display_job_name: 'Restore',
        display_tag_name: null,
        target_runs: [
          {
            id: 201,
            run_id: 99,
            target_id: body.destination_target_id,
            status: 'success',
            operation: 'restore',
            started_at: nowIso,
            finished_at: nowIso,
            artifact_path: body.artifact_path,
          },
        ],
      }, { status: 201 })
    }
    return HttpResponse.json({ error: 'Invalid request' }, { status: 400 })
  }),
]
