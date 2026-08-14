import React from 'react'
import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import DashboardPage from '../Dashboard'

// Simplify framer-motion in tests and strip animation props
vi.mock('framer-motion', () => {
  type MotionTestProps = React.PropsWithChildren<Record<string, unknown>>
  const passthrough = (Tag: keyof React.JSX.IntrinsicElements) => ({ children, ...rest }: MotionTestProps) => {
    // Remove animation-related props to avoid DOM warnings
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { initial, animate, transition, whileHover, whileTap, whileFocus, exit, ...others } = rest || {}
    return React.createElement(Tag, others, children)
  }
  return { motion: { div: passthrough('div'), section: passthrough('section') } }
})

// Mock API client used by the page
vi.mock('../../api/client', () => {
  return {
    api: {
      listTargets: vi.fn(),
      listJobs: vi.fn(),
      listPlugins: vi.fn(),
      listRuns: vi.fn(),
      upcomingJobs: vi.fn(),
      listProtection: vi.fn(),
    },
  }
})

// Import the mocked api to configure behaviors per test
import { api } from '../../api/client'

function renderWithClient(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([
    { path: '/', element: ui },
    { path: '/targets', element: <div>Targets</div> },
    { path: '/jobs', element: <div>Jobs</div> },
    { path: '/runs', element: <div>Runs</div> },
  ], { initialEntries: ['/'] })
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
}

describe('DashboardPage', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.listProtection).mockResolvedValue([])
  })

  it('shows KPI numbers based on API responses and computes success rate', async () => {
    // Arrange API mocks
    vi.mocked(api.listTargets).mockResolvedValue([{ id: 1, name: 'T1', slug: 't1', created_at: '', updated_at: '' }])
    vi.mocked(api.listJobs).mockResolvedValue([
      { id: 10, tag_id: 101, name: 'Job A', schedule_cron: '* * * * *', enabled: true, created_at: '', updated_at: '' },
    ])
    vi.mocked(api.listPlugins).mockResolvedValue([
      { key: 'local-files', name: 'Local Files', version: '1.0.0' },
      { key: 's3', name: 'Amazon S3', version: '1.0.0' },
      { key: 'gdrive', name: 'Google Drive', version: '1.0.0' },
    ])
    vi.mocked(api.listRuns).mockImplementation((params) => {
      if (params && params.start_date) {
        // Last 24h stats: 2 runs, 1 success
        return Promise.resolve([
          { id: 1, job_id: 10, status: 'success', started_at: new Date().toISOString(), finished_at: new Date().toISOString(), job: { id: 10, tag_id: 101, name: 'Job A', schedule_cron: '* * * * *', enabled: true, created_at: '', updated_at: '' } },
          { id: 2, job_id: 10, status: 'failed', started_at: new Date().toISOString(), finished_at: new Date().toISOString(), job: { id: 10, tag_id: 101, name: 'Job A', schedule_cron: '* * * * *', enabled: true, created_at: '', updated_at: '' } },
        ])
      }
      // Recent runs list (3 items)
      return Promise.resolve([
        { id: 3, job_id: 10, status: 'success', started_at: new Date().toISOString(), finished_at: new Date().toISOString(), job: { id: 10, tag_id: 101, name: 'Job A', schedule_cron: '* * * * *', enabled: true, created_at: '', updated_at: '' } },
        { id: 4, job_id: 10, status: 'failed', started_at: new Date().toISOString(), finished_at: new Date().toISOString(), job: { id: 10, tag_id: 101, name: 'Job B', schedule_cron: '* * * * *', enabled: true, created_at: '', updated_at: '' } },
        { id: 5, job_id: 10, status: 'running', started_at: new Date().toISOString(), finished_at: null, job: { id: 10, tag_id: 101, name: 'Job C', schedule_cron: '* * * * *', enabled: true, created_at: '', updated_at: '' } },
      ])
    })
    vi.mocked(api.upcomingJobs).mockResolvedValue([
      { job_id: 10, name: 'Backup Daily', next_run_at: new Date(Date.now() + 60_000).toISOString() },
      { job_id: 11, name: 'Backup Weekly', next_run_at: new Date(Date.now() + 120_000).toISOString() },
    ])

    renderWithClient(<DashboardPage />)

    // Targets card shows "1"
    const targetsLabel = await screen.findByText('Targets')
    const targetsCard = targetsLabel.parentElement!.parentElement as HTMLElement
    expect(within(targetsCard).getByText('1')).toBeInTheDocument()

    // Jobs card shows "1"
    const jobsLabel = screen.getByText('Jobs')
    const jobsCard = jobsLabel.parentElement!.parentElement as HTMLElement
    expect(within(jobsCard).getByText('1')).toBeInTheDocument()

    // Plugins card shows "3"
    const pluginsLabel = screen.getByText('Plugins')
    const pluginsCard = pluginsLabel.parentElement!.parentElement as HTMLElement
    expect(within(pluginsCard).getByText('3')).toBeInTheDocument()

    // Runs (24h) shows "2"
    const runsLabel = screen.getByText('Runs (24h)')
    const runsCard = runsLabel.parentElement!.parentElement as HTMLElement
    expect(within(runsCard).getByText('2')).toBeInTheDocument()

    // Success rate shows 50%
    await screen.findByText('Success rate')
    expect(screen.getByText('50%')).toBeInTheDocument()

    // Recent Runs list shows job names
    await waitFor(() => {
      expect(screen.getByText('Job A')).toBeInTheDocument()
      expect(screen.getByText('Job B')).toBeInTheDocument()
    })

    // Upcoming Jobs list shows items
    await waitFor(() => {
      expect(screen.getByText('Backup Daily')).toBeInTheDocument()
      expect(screen.getByText('Backup Weekly')).toBeInTheDocument()
    })

    // Ensure listRuns was invoked both for 24h window and recent list
    expect(vi.mocked(api.listRuns).mock.calls.length).toBeGreaterThanOrEqual(2)
    const firstCallArg = vi.mocked(api.listRuns).mock.calls.find(([params]) => params?.start_date)
    expect(firstCallArg).toBeTruthy()
  })

  it('shows protection facts and exact gap reasons for every target', async () => {
    vi.mocked(api.listTargets).mockResolvedValue([])
    vi.mocked(api.listJobs).mockResolvedValue([])
    vi.mocked(api.listPlugins).mockResolvedValue([])
    vi.mocked(api.listRuns).mockResolvedValue([])
    vi.mocked(api.upcomingJobs).mockResolvedValue([])
    vi.mocked(api.listProtection).mockResolvedValue([
      {
        target_id: 1,
        target_name: 'PostgreSQL',
        target_slug: 'postgresql',
        plugin_name: 'postgresql',
        covering_jobs: [{ job_id: 10, name: 'Nightly databases', schedule_cron: '0 2 * * *', next_run_at: '2026-08-15T02:00:00Z' }],
        latest_attempt: { run_id: 20, target_run_id: 30, started_at: '2026-08-14T01:00:00Z', finished_at: '2026-08-14T01:01:00Z', status: 'failed', message: 'connection refused' },
        latest_success: { run_id: 19, target_run_id: 29, finished_at: '2026-08-13T01:01:00Z', artifact_path: '/backups/postgresql/dump.sql', artifact_bytes: 128, sha256: 'a'.repeat(64), age_seconds: 90000 },
        next_run_at: '2026-08-15T02:00:00Z',
        consecutive_failures: 2,
        gap_reason: 'scheduled_backup_missing',
      },
      {
        target_id: 2,
        target_name: 'Pi-hole',
        target_slug: 'pi-hole',
        plugin_name: 'pihole',
        covering_jobs: [],
        latest_attempt: null,
        latest_success: null,
        next_run_at: null,
        consecutive_failures: 0,
        gap_reason: 'not_scheduled',
      },
      {
        target_id: 3,
        target_name: 'Vaultwarden',
        target_slug: 'vaultwarden',
        plugin_name: 'vaultwarden',
        covering_jobs: [{ job_id: 11, name: 'Nightly apps', schedule_cron: '0 3 * * *', next_run_at: '2026-08-15T03:00:00Z' }],
        latest_attempt: null,
        latest_success: null,
        next_run_at: '2026-08-15T03:00:00Z',
        consecutive_failures: 0,
        gap_reason: 'never_succeeded',
      },
    ])

    renderWithClient(<DashboardPage />)

    expect(await screen.findByRole('heading', { name: 'Backup protection' })).toBeInTheDocument()
    const postgresqlRow = screen.getByText('PostgreSQL').closest('tr') as HTMLElement
    expect(within(postgresqlRow).getByText('Scheduled backup missing')).toBeInTheDocument()
    expect(within(postgresqlRow).getByText('2')).toBeInTheDocument()
    expect(within(postgresqlRow).getByText('Nightly databases')).toBeInTheDocument()
    expect(screen.getByText('Not scheduled')).toBeInTheDocument()
    expect(screen.getByText('Never succeeded')).toBeInTheDocument()
  })

  it('does not present an API failure as an empty target list', async () => {
    vi.mocked(api.listTargets).mockResolvedValue([])
    vi.mocked(api.listJobs).mockResolvedValue([])
    vi.mocked(api.listPlugins).mockResolvedValue([])
    vi.mocked(api.listRuns).mockResolvedValue([])
    vi.mocked(api.upcomingJobs).mockResolvedValue([])
    vi.mocked(api.listProtection).mockRejectedValue(new Error('unavailable'))

    renderWithClient(<DashboardPage />)

    expect(await screen.findByText('Protection facts could not be loaded.')).toBeInTheDocument()
    expect(screen.queryByText('No targets configured.')).not.toBeInTheDocument()
  })
})
