import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import TargetSchedulePage from './TargetSchedule'

vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
  // Plugins
  if (url.toString().endsWith('/plugins/')) {
    return new Response(JSON.stringify([
      { key: 'pihole', name: 'Pi-hole', version: '1.0.0' },
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
  // Create job
  if (url.toString().endsWith('/jobs/')) {
    if (init?.method === 'POST') {
      return new Response(JSON.stringify({
        id: 9,
        target_id: 1,
        name: 'Pi-hole Backup',
        schedule_cron: '0 2 * * *',
        enabled: 'true',
        plugin: 'pihole',
        plugin_version: '1.0.0',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }), { status: 201 })
    }
  }
  return new Response('not found', { status: 404 })
}))

function renderWithProviders(route: string) {
  const qc = new QueryClient()
  const router = createMemoryRouter([
    { path: '/targets/:id/schedule', element: <TargetSchedulePage /> },
  ], { initialEntries: [route] })
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  )
}

describe('TargetSchedulePage', () => {
  it('creates a schedule for a target', async () => {
    renderWithProviders('/targets/1/schedule')
    await screen.findByText('Schedule Backup')
    // Defaults should be present; submit
    fireEvent.submit((await screen.findByText('Create Schedule')).closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
  })
})


