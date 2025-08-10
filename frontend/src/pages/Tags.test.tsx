import React from 'react'
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import TagsPage from './Tags'
import { ConfirmProvider } from '../components/ConfirmProvider'

async function defaultFetchStub(url: string, init?: RequestInit) {
  const u = url.toString()
  // List tags / create / delete
  if (u.endsWith('/tags/')) {
    if (init?.method === 'POST') {
      const body = JSON.parse(init.body as string)
      return new Response(JSON.stringify({ id: 103, slug: body.name.toLowerCase(), display_name: body.name, created_at: new Date().toISOString(), updated_at: new Date().toISOString() }), { status: 201 })
    }
    if (init?.method === 'DELETE') {
      return new Response('', { status: 204 })
    }
    return new Response(JSON.stringify([
      { id: 101, slug: 'pihole', display_name: 'Pi-hole', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
      { id: 102, slug: 'linux', display_name: 'Linux', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    ]), { status: 200 })
  }
  // Targets list to detect AUTO tags
  if (u.endsWith('/targets/')) {
    return new Response(JSON.stringify([
      { id: 11, name: 'Pi-hole', slug: 'pihole', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    ]), { status: 200 })
  }
  // Tag targets listing
  if (u.endsWith('/tags/101/targets')) {
    return new Response(JSON.stringify([
      {
        target: { id: 11, name: 'Pi-hole', slug: 'pihole', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        origin: 'AUTO',
        source_group_id: null,
      },
      {
        target: { id: 12, name: 'NAS', slug: 'nas', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        origin: 'GROUP',
        source_group_id: 5,
      },
    ]), { status: 200 })
  }
  if (u.endsWith('/tags/102/targets')) {
    return new Response(JSON.stringify([]), { status: 200 })
  }
  return new Response('not found', { status: 404 })
}

vi.stubGlobal('fetch', vi.fn(defaultFetchStub))

function renderWithProviders(route: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([
    { path: '/tags', element: <TagsPage /> },
  ], { initialEntries: [route] })
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <RouterProvider router={router} />
      </ConfirmProvider>
    </QueryClientProvider>
  )
}

describe('TagsPage', () => {
  beforeEach(() => {
    ;(fetch as any).mockClear?.()
  })
  afterEach(() => cleanup())

  it('lists tags and shows attachments with filters', async () => {
    renderWithProviders('/tags')
    await screen.findByText('Tags')
    await screen.findAllByText('Pi-hole')
    // Select first tag explicitly to trigger attachments load
    const tagSelect = await screen.findByLabelText('Select Tag')
    fireEvent.change(tagSelect, { target: { value: '101' } })
    await screen.findByText('Target')
    await screen.findAllByText('Pi-hole')
    await screen.findByText('NAS')

    // Apply origin filter
    const originSelect = await screen.findByLabelText('Origin')
    fireEvent.change(originSelect, { target: { value: 'AUTO' } })
    // NAS (GROUP) should be filtered out
    await waitFor(() => {
      expect(screen.queryByText('NAS')).toBeNull()
      expect(screen.getAllByText('Pi-hole').length).toBeGreaterThan(0)
    })
  })

  it('hides delete button for AUTO tag and deletes a manual tag', async () => {
    renderWithProviders('/tags')
    // Delete button should NOT exist for AUTO tag (Pi-hole)
    await screen.findAllByText('Pi-hole')
    expect(screen.queryByLabelText('Delete tag Pi-hole')).toBeNull()
    // But exists for Linux
    const delBtn = await screen.findByLabelText('Delete tag Linux')
    fireEvent.click(delBtn)
    const ok = await screen.findByRole('button', { name: 'OK' })
    fireEvent.click(ok)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => /\/tags\/.+/.test(c[0]) && c[1]?.method === 'DELETE')).toBe(true))
  })

  it('creates a tag from the create form', async () => {
    renderWithProviders('/tags')
    await screen.findByText('Tags')
    fireEvent.click(screen.getByLabelText('Add Tag'))
    const nameInput = (await screen.findByLabelText('Name')) as HTMLInputElement
    fireEvent.change(nameInput, { target: { value: 'Databases' } })
    fireEvent.submit(nameInput.closest('form')!)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/tags/') && c[1]?.method === 'POST')).toBe(true))
  })
})


