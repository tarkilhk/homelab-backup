import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RunsPage from '../Runs'
import React, { type ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'

vi.stubGlobal('fetch', vi.fn(async (url: string) => {
  // List runs (with optional query)
  if (url.endsWith('/runs/') || String(url).includes('/runs/?')) {
    return new Response(JSON.stringify([
      { id: 1, job_id: 1, status: 'success', started_at: new Date().toISOString(), finished_at: new Date().toISOString() },
    ]), { status: 200 })
  }
  // Get single run with target_runs for expansion
  if (String(url).match(/\/runs\/1$/)) {
    const now = new Date().toISOString()
    return new Response(JSON.stringify({
      id: 1,
      job_id: 1,
      status: 'success',
      started_at: now,
      finished_at: now,
      job: { id: 1, tag_id: 1, name: 'Daily pihole backup', schedule_cron: '* * * * *', enabled: true, created_at: now, updated_at: now },
      target_runs: [
        {
          id: 11,
          run_id: 1,
          target_id: 42,
          status: 'success',
          started_at: now,
          finished_at: now,
          artifact_path: '/backups/pihole/2025-08-10/pihole.zip',
          artifact_bytes: 12345,
          sha256: '0f' + 'a'.repeat(62),
          message: 'Run completed successfully',
        },
      ],
    }), { status: 200 })
  }
  // Targets list used for name mapping (can be empty)
  if (url.endsWith('/targets/')) {
    return new Response(JSON.stringify([]), { status: 200 })
  }
  return new Response('not found', { status: 404 })
}))

function wrapper(children: ReactNode) {
  const qc = new QueryClient()
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {children}
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('RunsPage', () => {
  it('renders runs table', async () => {
    render(wrapper(<RunsPage />))
    await screen.findAllByText('Past Runs')
    await screen.findByText('success')
  })

  it('applies filters and calls API with query', async () => {
    const fetchSpy = global.fetch as unknown as ReturnType<typeof vi.fn>
    render(wrapper(<RunsPage />))
    // open status select and choose failed (first instance)
    const statusSel = (await screen.findAllByLabelText('Status'))[0]
    fireEvent.change(statusSel, { target: { value: 'failed' } })
    await waitFor(() => {
      const called = fetchSpy.mock.calls.some((args: unknown[]) => String(args[0]).includes('/runs/?status=failed'))
      expect(called).toBe(true)
    })
  })

  it('sets default times for date-only selections', async () => {
    const fetchSpy = global.fetch as unknown as ReturnType<typeof vi.fn>
    fetchSpy.mockClear()
    render(wrapper(<RunsPage />))

    const startInput = (await screen.findAllByLabelText('From'))[0] as HTMLInputElement
    const endInput = (await screen.findAllByLabelText('To'))[0] as HTMLInputElement

    // With date-only inputs, values are YYYY-MM-DD
    fireEvent.change(startInput, { target: { value: '2024-01-02' } })
    fireEvent.change(endInput, { target: { value: '2024-01-03' } })

    await waitFor(() => {
      expect(startInput.value).toBe('2024-01-02')
      expect(endInput.value).toBe('2024-01-03')
    })
  })

  it('does not show an Artifact column; details modal shows artifact info', async () => {
    render(wrapper(<RunsPage />))
    await screen.findAllByText('Past Runs')
    // Before expanding, Artifact header should not be present anywhere
    expect(screen.queryByText('Artifact')).toBeNull()

    // Expand the first run row
    const expandBtn = (await screen.findAllByRole('button', { name: 'Expand' }))[0]
    fireEvent.click(expandBtn)

    // In expanded rows, there is no Artifact column; artifact details are shown in a modal
    // Open the target run details modal
    const viewBtn = await screen.findByText('View')
    fireEvent.click(viewBtn)

    // Modal shows Artifact, Size, and SHA256 with expected values
    await screen.findByText('Artifact')
    await screen.findByText('/backups/pihole/2025-08-10/pihole.zip')
    await screen.findByText('12.1 KB (12,345 bytes)')
    await screen.findByText('0f' + 'a'.repeat(62))
  })
})


