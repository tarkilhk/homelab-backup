import React from 'react'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import JobsPage from './Jobs'

vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
  // List targets (for global jobs page)
  if (url.toString().endsWith('/targets/')) {
    return new Response(JSON.stringify([
      {
        id: 1,
        name: 'Pi-hole',
        slug: 'pihole',
        plugin_name: 'pihole',
        plugin_config_json: '{}',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]), { status: 200 })
  }
  // Get target
  if (url.toString().endsWith('/targets/1')) {
    return new Response(JSON.stringify({
      id: 1,
      name: 'Pi-hole',
      slug: 'pihole',
      plugin_name: 'pihole',
      plugin_config_json: '{}',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }), { status: 200 })
  }
  // Jobs collection
  if (url.toString().endsWith('/jobs/')) {
    if (init?.method === 'POST') {
      return new Response(JSON.stringify({
        id: 9,
        target_id: 1,
        name: 'Pi-hole Backup',
        schedule_cron: '0 2 * * *',
        enabled: 'true',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }), { status: 201 })
    }
    // Treat undefined method as GET
    // Provide one existing job for actions tests
    return new Response(JSON.stringify([
      {
        id: 9,
        target_id: 1,
        name: 'Pi-hole Backup',
        schedule_cron: '0 2 * * *',
        enabled: 'true',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]), { status: 200 })
  }
  // Run now
  if (url.toString().endsWith('/jobs/9/run') && init?.method === 'POST') {
    return new Response(JSON.stringify({
      id: 101,
      job_id: 9,
      started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      status: 'success',
      message: 'manual run ok',
    }), { status: 200 })
  }
  // Update job
  if (url.toString().endsWith('/jobs/9') && init?.method === 'PUT') {
    return new Response(JSON.stringify({
      id: 9,
      target_id: 1,
      name: 'Updated Name',
      schedule_cron: '0 2 * * *',
      enabled: 'true',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }), { status: 200 })
  }
  // Delete job
  if (url.toString().endsWith('/jobs/9') && init?.method === 'DELETE') {
    return new Response('', { status: 204 })
  }
  return new Response('not found', { status: 404 })
}))

function renderWithProviders(route: string) {
  const qc = new QueryClient()
  const router = createMemoryRouter([
    { path: '/targets/:id/jobs', element: <JobsPage /> },
    { path: '/jobs', element: <JobsPage /> },
    { path: '/targets', element: <div>Targets</div> },
  ], { initialEntries: [route] })
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
}

describe('JobsPage', () => {
  afterEach(() => cleanup())
  it('creates a job for a target', async () => {
    renderWithProviders('/targets/1/jobs')
    await screen.findByText('Jobs')
    // Defaults should be present without typing
    const nameInput = (await screen.findByLabelText('Job Name')) as HTMLInputElement
    expect(nameInput.value).toBe('Pi-hole Backup')
    const cronInput = (await screen.findByLabelText('Cron')) as HTMLInputElement
    expect(cronInput.value).toBe('0 2 * * *')
    fireEvent.submit((await screen.findByText('Create Job')).closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
  })

  it('creates a job from the global page with target picker', async () => {
    renderWithProviders('/jobs')
    await screen.findByText('Jobs')
    // Ensure target picker is present and select explicitly
    const picker = await screen.findByLabelText('Target')
    fireEvent.change(picker, { target: { value: '1' } })
    // Job name should prefill from selected target
    const nameInput = (await screen.findByLabelText('Job Name')) as HTMLInputElement
    expect(nameInput.value).toBe('Pi-hole Backup')
    // Selecting a daily cron should prefix the name
    const cronInput = (await screen.findByLabelText('Cron')) as HTMLInputElement
    fireEvent.change(cronInput, { target: { value: '0 2 * * *' } })
    await waitFor(() => expect((screen.getByLabelText('Job Name') as HTMLInputElement).value).toBe('Daily Pi-hole Backup'))
    fireEvent.submit((await screen.findByText('Create Job')).closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
  })

  it('shows edit/delete actions and supports editing a job', async () => {
    renderWithProviders('/targets/1/jobs')
    await screen.findByText('Existing Jobs')
    await screen.findByText('Pi-hole Backup')
    const editBtn = await screen.findByLabelText('Edit')
    fireEvent.click(editBtn)
    await screen.findByText('Save')
    const nameInput = (await screen.findByLabelText('Job Name')) as HTMLInputElement
    expect(nameInput.value).toBe('Pi-hole Backup')
    fireEvent.submit((await screen.findByText('Save')).closest('form')!)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/jobs/9') && c[1]?.method === 'PUT')).toBe(true))
  })

  it('deletes a job via trash icon', async () => {
    vi.spyOn(window, 'confirm').mockReturnValueOnce(true as unknown as boolean)
    renderWithProviders('/targets/1/jobs')
    await screen.findByText('Existing Jobs')
    await screen.findByText('Pi-hole Backup')
    const delBtn = await screen.findByLabelText('Delete')
    fireEvent.click(delBtn)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/jobs/9') && c[1]?.method === 'DELETE')).toBe(true))
  })

  it('runs a job immediately via the play icon', async () => {
    renderWithProviders('/targets/1/jobs')
    await screen.findByText('Existing Jobs')
    await screen.findByText('Pi-hole Backup')
    const runBtn = await screen.findByLabelText('Run now')
    fireEvent.click(runBtn)
    await waitFor(() =>
      expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/jobs/9/run') && c[1]?.method === 'POST')).toBe(true)
    )
  })
})


