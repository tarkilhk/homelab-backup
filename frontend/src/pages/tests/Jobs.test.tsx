import React from 'react'
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import JobsPage from '../Jobs'
import { ConfirmProvider } from '../../components/ConfirmProvider'

async function defaultFetchStub(url: string, init?: RequestInit) {
  const u = url.toString()
  // Tags (global selection)
  if (u.endsWith('/tags/')) {
    return new Response(JSON.stringify([
      { id: 101, slug: 'pihole', display_name: 'Pi-hole', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
      { id: 102, slug: 'linux', display_name: 'Linux', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    ]), { status: 200 })
  }
  // Get target (for target-specific page title + defaults)
  if (u.endsWith('/targets/1')) {
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
  // Target tags (to pick AUTO tag)
  if (u.endsWith('/targets/1/tags')) {
    return new Response(JSON.stringify([
      {
        tag: { id: 101, slug: 'pihole', display_name: 'Pi-hole', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        origin: 'AUTO',
        source_group_id: null,
      },
      // may include others, but AUTO is key
    ]), { status: 200 })
  }
  // Jobs collection
  if (u.endsWith('/jobs/')) {
    if (init?.method === 'POST') {
      return new Response(JSON.stringify({
        id: 9,
        tag_id: 101,
        name: 'Pi-hole Backup',
        schedule_cron: '0 2 * * *',
        enabled: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }), { status: 201 })
    }
    // Treat undefined method as GET
    // Provide one existing job for actions tests
    return new Response(JSON.stringify([
      {
        id: 9,
        tag_id: 101,
        name: 'Pi-hole Backup',
        schedule_cron: '0 2 * * *',
        enabled: true,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]), { status: 200 })
  }
  // Run now
  if (u.endsWith('/jobs/9/run') && init?.method === 'POST') {
    return new Response(JSON.stringify({
      id: 101,
      job_id: 9,
      started_at: new Date().toISOString(),
      finished_at: null,
      status: 'running',
      message: 'manual run ok',
    }), { status: 200 })
  }
  // Update job
  if (u.endsWith('/jobs/9') && init?.method === 'PUT') {
    return new Response(JSON.stringify({
      id: 9,
      tag_id: 101,
      name: 'Updated Name',
      schedule_cron: '0 2 * * *',
      enabled: true,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }), { status: 200 })
  }
  // Delete job
  if (u.endsWith('/jobs/9') && init?.method === 'DELETE') {
    return new Response('', { status: 204 })
  }
  return new Response('not found', { status: 404 })
}

vi.stubGlobal('fetch', vi.fn(defaultFetchStub))

function renderWithProviders(route: string) {
  const qc = new QueryClient()
  const router = createMemoryRouter([
    { path: '/targets/:id/jobs', element: <JobsPage /> },
    { path: '/jobs', element: <JobsPage /> },
    { path: '/targets', element: <div>Targets</div> },
  ], { initialEntries: [route] })
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <RouterProvider router={router} />
      </ConfirmProvider>
    </QueryClientProvider>
  )
}

describe('JobsPage', () => {
  beforeEach(() => {
    // Clear only call history, keep default implementation
    ;(fetch as any).mockClear?.()
  })
  afterEach(() => cleanup())
  
  it('creates a job for a target (uses auto tag)', async () => {
    renderWithProviders('/targets/1/jobs')
    await screen.findByText('Jobs')
    // Open editor
    fireEvent.click(screen.getByLabelText('Add Job'))
    // Defaults should be present without typing
    const nameInput = (await screen.findByLabelText('Job Name')) as HTMLInputElement
    // Name is seeded from target and should not auto-change until user asks
    await waitFor(() => expect(nameInput.value).toBe('Pi-hole Backup'))
    const cronInput = (await screen.findByLabelText('Cron')) as HTMLInputElement
    expect(cronInput.value).toBe('0 2 * * *')
    // Changing cron alone does not auto-change name now
    fireEvent.change(cronInput, { target: { value: '0 0 1 * *' } })
    await waitFor(() => expect(nameInput.value).toBe('Pi-hole Backup'))
    // Clicking suggest applies the cadence + suffix
    fireEvent.click(await screen.findByLabelText('Suggest name'))
    await waitFor(() => expect(nameInput.value).toBe('Monthly Pi-hole Backup'))
    fireEvent.submit((await screen.findByText('Create Job')).closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    // Assert payload shape: tag_id from AUTO tag and boolean enabled
    const postCall = (fetch as any).mock.calls.find((c: any[]) => c[0].toString().endsWith('/jobs/') && c[1]?.method === 'POST')
    expect(postCall).toBeTruthy()
    const body = JSON.parse(postCall[1].body)
    expect(body.tag_id).toBe(101)
    expect(typeof body.enabled).toBe('boolean')
  })

  it('creates a job from the global page with tag picker', async () => {
    renderWithProviders('/jobs')
    await screen.findByText('Jobs')
    // Open editor first
    fireEvent.click(screen.getByLabelText('Add Job'))
    // Ensure tag picker is present and select explicitly
    const picker = await screen.findByLabelText('Tag')
    fireEvent.change(picker, { target: { value: '101' } })
    // Selecting a daily cron should prefix the name
    const cronInput = (await screen.findByLabelText('Cron')) as HTMLInputElement
    fireEvent.change(cronInput, { target: { value: '0 2 * * *' } })
    // Name is not auto-derived from tag; ensure form still accepts submission
    // Human readable label should show
    await screen.findByText(/Every day at/i)
    fireEvent.submit((await screen.findByText('Create Job')).closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    // Assert payload shape: selected tag and boolean enabled
    const postCall = (fetch as any).mock.calls.find((c: any[]) => c[0].toString().endsWith('/jobs/') && c[1]?.method === 'POST')
    expect(postCall).toBeTruthy()
    const body = JSON.parse(postCall[1].body)
    expect(body.tag_id).toBe(101)
    expect(typeof body.enabled).toBe('boolean')
  })

  it('does not duplicate cadence prefix when cron changes repeatedly', async () => {
    renderWithProviders('/jobs')
    await screen.findByText('Jobs')
    fireEvent.click(screen.getByLabelText('Add Job'))
    const picker = await screen.findByLabelText('Tag')
    fireEvent.change(picker, { target: { value: '101' } })
    const nameInput = (await screen.findByLabelText('Job Name')) as HTMLInputElement
    const cronInput = (await screen.findByLabelText('Cron')) as HTMLInputElement
    // First set to daily and click suggest to apply prefix-only name
    fireEvent.change(cronInput, { target: { value: '0 2 * * *' } })
    fireEvent.click(await screen.findByLabelText('Suggest name'))
    await waitFor(() => expect(nameInput.value).toBe('Daily Pi-hole Backup'))
    // Change daily time again – should remain single prefix
    fireEvent.change(cronInput, { target: { value: '5 5 * * *' } })
    await waitFor(() => expect(nameInput.value).toBe('Daily Pi-hole Backup'))
    // Switch to weekly then back to daily; no duplication
    fireEvent.change(cronInput, { target: { value: '0 2 * * 1' } })
    await waitFor(() => expect(nameInput.value).toBe('Weekly Pi-hole Backup'))
    fireEvent.change(cronInput, { target: { value: '0 2 * * *' } })
    await waitFor(() => expect(nameInput.value).toBe('Daily Pi-hole Backup'))
  })

  it('does not change name automatically if user already typed one', async () => {
    renderWithProviders('/jobs')
    await screen.findByText('Jobs')
    fireEvent.click(screen.getByLabelText('Add Job'))
    const picker = await screen.findByLabelText('Tag')
    fireEvent.change(picker, { target: { value: '101' } })
    const nameInput = (await screen.findByLabelText('Job Name')) as HTMLInputElement
    // User types a custom name
    fireEvent.change(nameInput, { target: { value: 'My Special Backup' } })
    const cronInput = (await screen.findByLabelText('Cron')) as HTMLInputElement
    // Change cron a few times – name should remain unchanged
    fireEvent.change(cronInput, { target: { value: '0 2 * * *' } })
    await waitFor(() => expect(nameInput.value).toBe('My Special Backup'))
    fireEvent.change(cronInput, { target: { value: '0 0 1 * *' } })
    await waitFor(() => expect(nameInput.value).toBe('My Special Backup'))
    // Changing tag should also not modify a non-empty custom name
    fireEvent.change(picker, { target: { value: '102' } })
    await waitFor(() => expect(nameInput.value).toBe('My Special Backup'))
  })

  it('suggests a smart name when clicking the sparkles icon', async () => {
    renderWithProviders('/jobs')
    await screen.findByText('Jobs')
    fireEvent.click(screen.getByLabelText('Add Job'))
    const picker = await screen.findByLabelText('Tag')
    fireEvent.change(picker, { target: { value: '101' } })
    const cronInput = (await screen.findByLabelText('Cron')) as HTMLInputElement
    fireEvent.change(cronInput, { target: { value: '0 0 1 * *' } }) // monthly
    const suggestBtn = await screen.findByLabelText('Suggest name')
    fireEvent.click(suggestBtn)
    const nameInput = (await screen.findByLabelText('Job Name')) as HTMLInputElement
    await waitFor(() => expect(nameInput.value).toBe('Monthly Pi-hole Backup'))
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
    // Assert PUT payload contains boolean enabled and tag_id (from AUTO tag on target page)
    const putCall = (fetch as any).mock.calls.find((c: any[]) => c[0].toString().endsWith('/jobs/9') && c[1]?.method === 'PUT')
    expect(putCall).toBeTruthy()
    const putBody = JSON.parse(putCall[1].body)
    expect(typeof putBody.enabled).toBe('boolean')
    // tag_id may be omitted if unchanged; on target-specific page we include auto tag
    if (putBody.tag_id != null) expect(putBody.tag_id).toBe(101)
  })

  it('deletes a job via trash icon', async () => {
    // Simulate user confirming via our modal by clicking its confirm button
    renderWithProviders('/targets/1/jobs')
    await screen.findByText('Existing Jobs')
    await screen.findByText('Pi-hole Backup')
    const delBtn = await screen.findByLabelText('Delete')
    fireEvent.click(delBtn)
    // The confirm modal appears; click OK
    const ok = await screen.findByRole('button', { name: 'OK' })
    fireEvent.click(ok)
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

  it('requires selecting a tag on global page before creating', async () => {
    renderWithProviders('/jobs')
    await screen.findByText('Jobs')
    fireEvent.click(screen.getByLabelText('Add Job'))
    // Submit without selecting a tag
    fireEvent.submit((await screen.findByText('Create Job')).closest('form')!)
    await waitFor(() => {
      expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/jobs/') && c[1]?.method === 'POST')).toBe(false)
    })
    // Now select and submit
    const picker = await screen.findByLabelText('Tag')
    fireEvent.change(picker, { target: { value: '101' } })
    fireEvent.submit((await screen.findByText('Create Job')).closest('form')!)
    await waitFor(() => {
      expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/jobs/') && c[1]?.method === 'POST')).toBe(true)
    })
  })

  it('filters jobs by tag using the Filter Tag select', async () => {
    // Override fetch during this test so GET /jobs/ returns two jobs
    const origImpl = (fetch as any).getMockImplementation?.() || defaultFetchStub
    ;(fetch as any).mockImplementation(async (url: string, init?: RequestInit) => {
      const u = url.toString()
      if (u.endsWith('/jobs/') && !init?.method) {
        return new Response(JSON.stringify([
          { id: 9, tag_id: 101, name: 'Pi-hole Backup', schedule_cron: '0 2 * * *', enabled: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
          { id: 10, tag_id: 102, name: 'Linux Backup', schedule_cron: '0 3 * * *', enabled: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        ]), { status: 200 })
      }
      return origImpl(url, init)
    })
    renderWithProviders('/jobs')
    await screen.findByText('Existing Jobs')
    // Both jobs visible initially
    await screen.findByText('Pi-hole Backup')
    await screen.findByText('Linux Backup')
    // Filter by Pi-hole tag
    const filterSelect = await screen.findByLabelText('Filter Tag')
    fireEvent.change(filterSelect, { target: { value: '101' } })
    // Expect only Pi-hole row remains
    await waitFor(() => {
      expect(screen.getByText('Pi-hole Backup')).toBeTruthy()
      expect(screen.queryByText('Linux Backup')).toBeNull()
    })
    // Clear filters
    fireEvent.click(screen.getByText('Clear filters'))
    await waitFor(() => {
      expect(screen.getByText('Linux Backup')).toBeTruthy()
    })
    // Restore fetch
    ;(fetch as any).mockImplementation(origImpl)
  })

  it('falls back to — in Tag column when tags list is empty', async () => {
    // Force empty tags list and ensure one job exists
    const origImpl = (fetch as any).getMockImplementation?.() || defaultFetchStub
    ;(fetch as any).mockImplementation(async (url: string, init?: RequestInit) => {
      const u = url.toString()
      if (u.endsWith('/tags/')) {
        return new Response(JSON.stringify([]), { status: 200 })
      }
      if (u.endsWith('/jobs/') && !init?.method) {
        return new Response(JSON.stringify([
          { id: 9, tag_id: 101, name: 'Pi-hole Backup', schedule_cron: '0 2 * * *', enabled: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        ]), { status: 200 })
      }
      return origImpl(url, init)
    })
    renderWithProviders('/jobs')
    await screen.findByText('Existing Jobs')
    const row = (await screen.findByText('Pi-hole Backup')).closest('tr') as HTMLElement
    const cells = Array.from(row.querySelectorAll('td'))
    // Tag column is the second cell
    expect(cells[1].textContent?.trim()).toBe('—')
    // Restore fetch
    ;(fetch as any).mockImplementation(origImpl)
  })
})

