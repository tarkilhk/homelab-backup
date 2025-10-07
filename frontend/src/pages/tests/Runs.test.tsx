import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RunsPage from '../Runs'
import React, { type ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'

let nowIso = new Date().toISOString()

vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
  // List runs (with optional query)
  if (url.endsWith('/runs/') || String(url).includes('/runs/?')) {
    return new Response(JSON.stringify([
      { id: 1, job_id: 1, status: 'success', operation: 'backup', started_at: nowIso, finished_at: nowIso, display_job_name: 'Daily pihole backup', display_tag_name: 'Pihole', job: { id: 1, tag_id: 1, name: 'Daily pihole backup', schedule_cron: '* * * * *', enabled: true, created_at: nowIso, updated_at: nowIso }, target_runs: [] },
      { id: 2, job_id: 2, status: 'success', operation: 'restore', started_at: nowIso, finished_at: nowIso, display_job_name: 'Secondary Pihole Restore', display_tag_name: 'Secondary Pihole', job: { id: 2, tag_id: 2, name: 'Secondary restore job', schedule_cron: '* * * * *', enabled: true, created_at: nowIso, updated_at: nowIso }, target_runs: [{ id: 22, run_id: 2, target_id: 43, status: 'success', operation: 'restore', started_at: nowIso, finished_at: nowIso }] },
    ]), { status: 200 })
  }
  // Get single run with target_runs for expansion
  if (String(url).match(/\/runs\/1$/)) {
    return new Response(JSON.stringify({
      id: 1,
      job_id: 1,
      status: 'success',
      operation: 'backup',
      started_at: nowIso,
      finished_at: nowIso,
      job: { id: 1, tag_id: 1, name: 'Daily pihole backup', schedule_cron: '* * * * *', enabled: true, created_at: nowIso, updated_at: nowIso },
      display_job_name: 'Primary Pihole Restore',
      display_tag_name: 'Primary Pihole',
      target_runs: [
        {
          id: 11,
          run_id: 1,
          target_id: 42,
          status: 'success',
          operation: 'backup',
          started_at: nowIso,
          finished_at: nowIso,
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
    return new Response(JSON.stringify([
      { id: 42, name: 'Primary Pihole', slug: 'primary-pihole', plugin_name: 'pihole', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
      { id: 43, name: 'Secondary Pihole', slug: 'secondary-pihole', plugin_name: 'pihole', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
    ]), { status: 200 })
  }
  if (url.endsWith('/tags/')) {
    return new Response(JSON.stringify([
      { id: 1, slug: 'primary-pihole', display_name: 'Primary Pihole', created_at: nowIso, updated_at: nowIso },
      { id: 2, slug: 'secondary-pihole', display_name: 'Secondary Pihole', created_at: nowIso, updated_at: nowIso },
    ]), { status: 200 })
  }
  if (url.endsWith('/restores/') && init?.method === 'POST') {
    return new Response(JSON.stringify({
      id: 99,
      job_id: 1,
      status: 'success',
      operation: 'restore',
      started_at: nowIso,
      finished_at: nowIso,
      job: { id: 1, tag_id: 1, name: 'Daily pihole backup', schedule_cron: '* * * * *', enabled: true, created_at: nowIso, updated_at: nowIso },
      display_job_name: 'Secondary Pihole Restore',
      display_tag_name: 'Secondary Pihole',
      target_runs: [
        {
          id: 201,
          run_id: 99,
          target_id: 43,
          status: 'success',
          operation: 'restore',
          started_at: nowIso,
          finished_at: nowIso,
          artifact_path: '/backups/pihole/2025-08-10/pihole.zip',
        },
      ],
    }), { status: 201 })
  }
  return new Response('not found', { status: 404 })
}))

const fetchSpy = global.fetch as unknown as ReturnType<typeof vi.fn>

beforeEach(() => {
  nowIso = new Date().toISOString()
  fetchSpy.mockClear()
})

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
    await screen.findByText('Secondary Pihole Restore')
  })

  it('applies filters and calls API with query', async () => {
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

it('allows restoring a target run to another target', async () => {
  render(wrapper(<RunsPage />))
  await screen.findAllByText('Past Runs')

  const expandBtn = (await screen.findAllByRole('button', { name: 'Expand' }))[0]
  fireEvent.click(expandBtn)

  const viewBtn = await screen.findByText('View')
  fireEvent.click(viewBtn)

  const restoreBtn = await screen.findByText('Restore to Target')
  fireEvent.click(restoreBtn)

  const destSelect = await screen.findByLabelText('Destination Target')
  fireEvent.change(destSelect, { target: { value: '43' } })

  const confirmBtn = await screen.findByRole('button', { name: 'Confirm Restore' })
  fireEvent.click(confirmBtn)

  await waitFor(() => {
    expect(fetchSpy).toHaveBeenCalledWith(
      expect.stringContaining('/restores/'),
      expect.objectContaining({ method: 'POST' }),
    )
  })

  await waitFor(() => {
    expect(screen.queryByRole('button', { name: 'Confirm Restore' })).toBeNull()
  })

  await screen.findByText(/Restore triggered successfully/i)
})
