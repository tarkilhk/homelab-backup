import React from 'react'
import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DashboardPage from './Dashboard'

// Simplify framer-motion in tests and strip animation props
vi.mock('framer-motion', () => {
  const passthrough = (Tag: any) => ({ children, ...rest }: any) => {
    // Remove animation-related props to avoid DOM warnings
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { initial, animate, transition, whileHover, whileTap, whileFocus, exit, ...others } = rest || {}
    return <Tag {...others}>{children}</Tag>
  }
  return { motion: { div: passthrough('div'), section: passthrough('section') } }
})

// Mock API client used by the page
vi.mock('../api/client', () => {
  return {
    api: {
      listTargets: vi.fn(),
      listJobs: vi.fn(),
      listRuns: vi.fn(),
      upcomingJobs: vi.fn(),
    },
  }
})

// Import the mocked api to configure behaviors per test
import { api } from '../api/client'

function renderWithClient(ui: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

describe('DashboardPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows KPI numbers based on API responses and computes success rate', async () => {
    // Arrange API mocks
    ;(api.listTargets as any).mockResolvedValue([{ id: 1, name: 'T1', slug: 't1', created_at: '', updated_at: '' }])
    ;(api.listJobs as any).mockResolvedValue([
      { id: 10, target_id: 1, name: 'Job A', schedule_cron: '* * * * *', enabled: 'true', created_at: '', updated_at: '' },
    ])
    ;(api.listRuns as any).mockImplementation((params?: any) => {
      if (params && params.start_date) {
        // Last 24h stats: 2 runs, 1 success
        return Promise.resolve([
          { id: 1, job_id: 10, status: 'success', started_at: new Date().toISOString(), finished_at: new Date().toISOString(), job: { id: 10, target_id: 1, name: 'Job A', schedule_cron: '* * * * *', enabled: 'true', created_at: '', updated_at: '' } },
          { id: 2, job_id: 10, status: 'failed', started_at: new Date().toISOString(), finished_at: new Date().toISOString(), job: { id: 10, target_id: 1, name: 'Job A', schedule_cron: '* * * * *', enabled: 'true', created_at: '', updated_at: '' } },
        ])
      }
      // Recent runs list (3 items)
      return Promise.resolve([
        { id: 3, job_id: 10, status: 'success', started_at: new Date().toISOString(), finished_at: new Date().toISOString(), job: { id: 10, target_id: 1, name: 'Job A', schedule_cron: '* * * * *', enabled: 'true', created_at: '', updated_at: '' } },
        { id: 4, job_id: 10, status: 'failed', started_at: new Date().toISOString(), finished_at: new Date().toISOString(), job: { id: 10, target_id: 1, name: 'Job B', schedule_cron: '* * * * *', enabled: 'true', created_at: '', updated_at: '' } },
        { id: 5, job_id: 10, status: 'running', started_at: new Date().toISOString(), finished_at: null, job: { id: 10, target_id: 1, name: 'Job C', schedule_cron: '* * * * *', enabled: 'true', created_at: '', updated_at: '' } },
      ])
    })
    ;(api.upcomingJobs as any).mockResolvedValue([
      { job_id: 10, name: 'Backup Daily', target_id: 1, next_run_at: new Date(Date.now() + 60_000).toISOString() },
      { job_id: 11, name: 'Backup Weekly', target_id: 1, next_run_at: new Date(Date.now() + 120_000).toISOString() },
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
    expect((api.listRuns as any).mock.calls.length).toBeGreaterThanOrEqual(2)
    const firstCallArg = (api.listRuns as any).mock.calls.find((args: any[]) => args[0] && args[0].start_date)
    expect(firstCallArg).toBeTruthy()
  })
})


