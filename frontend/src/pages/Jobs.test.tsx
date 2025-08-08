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
    if (!init || init.method === 'GET') {
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
    // Defaults should be present; submit
    fireEvent.change(await screen.findByLabelText('Job Name'), { target: { value: 'Pi-hole Backup' } })
    fireEvent.change(await screen.findByLabelText('Cron'), { target: { value: '0 2 * * *' } })
    fireEvent.submit((await screen.findByText('Create Job')).closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
  })

  it('creates a job from the global page with target picker', async () => {
    renderWithProviders('/jobs')
    await screen.findByText('Jobs')
    // Ensure target picker is present and select explicitly
    const picker = await screen.findByLabelText('Target')
    fireEvent.change(picker, { target: { value: '1' } })
    // Fill required fields explicitly
    fireEvent.change(await screen.findByLabelText('Job Name'), { target: { value: 'Pi-hole Backup' } })
    fireEvent.change(await screen.findByLabelText('Cron'), { target: { value: '0 2 * * *' } })
    fireEvent.submit((await screen.findByText('Create Job')).closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
  })

  it('shows edit/delete actions and supports editing a job', async () => {
    renderWithProviders('/targets/1/jobs')
    await screen.findByText('Existing Jobs')
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
    const delBtn = await screen.findByLabelText('Delete')
    fireEvent.click(delBtn)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/jobs/9') && c[1]?.method === 'DELETE')).toBe(true))
  })
})


