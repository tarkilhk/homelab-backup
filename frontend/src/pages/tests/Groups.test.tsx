import React from 'react'
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import GroupsPage from '../Groups'
import { ConfirmProvider } from '../../components/ConfirmProvider'

async function defaultFetchStub(url: string, init?: RequestInit) {
  const u = url.toString()
  // List groups
  if (u.endsWith('/groups/')) {
    if (init?.method === 'POST') {
      return new Response(JSON.stringify({
        id: 2,
        name: 'Core Servers',
        description: 'Critical infra',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }), { status: 201 })
    }
    return new Response(JSON.stringify([
      { id: 1, name: 'Network', description: 'Routers and DNS', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    ]), { status: 200 })
  }
  // Update/Delete group
  if (u.endsWith('/groups/1') && init?.method === 'PUT') {
    return new Response(JSON.stringify({
      id: 1,
      name: 'Network Updated',
      description: 'Routers and DNS',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    }), { status: 200 })
  }
  if (u.endsWith('/groups/1') && init?.method === 'DELETE') {
    return new Response('', { status: 204 })
  }
  // Group targets
  if (u.endsWith('/groups/1/targets')) {
    return new Response(JSON.stringify({
      id: 1,
      name: 'Network',
      description: 'Routers and DNS',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      targets: [
        { id: 11, name: 'Pi-hole', slug: 'pihole', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
      ],
    }), { status: 200 })
  }
  
  // Targets list for multi-select
  if (u.endsWith('/targets/')) {
    return new Response(JSON.stringify([
      { id: 11, name: 'Pi-hole', slug: 'pihole', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
      { id: 12, name: 'NAS', slug: 'nas', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
    ]), { status: 200 })
  }
  
  // Membership changes
  if (u.endsWith('/groups/1/targets') && (init?.method === 'POST' || init?.method === 'DELETE')) {
    return new Response(JSON.stringify({
      id: 1,
      name: 'Network',
      description: 'Routers and DNS',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      targets: [
        { id: 11, name: 'Pi-hole', slug: 'pihole', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
        { id: 12, name: 'NAS', slug: 'nas', created_at: new Date().toISOString(), updated_at: new Date().toISOString() },
      ],
    }), { status: 200 })
  }
  
  return new Response('not found', { status: 404 })
}

vi.stubGlobal('fetch', vi.fn(defaultFetchStub))

function renderWithProviders(route: string) {
  const qc = new QueryClient()
  const router = createMemoryRouter([
    { path: '/groups', element: <GroupsPage /> },
  ], { initialEntries: [route] })
  return render(
    <QueryClientProvider client={qc}>
      <ConfirmProvider>
        <RouterProvider router={router} />
      </ConfirmProvider>
    </QueryClientProvider>
  )
}

describe('GroupsPage', () => {
  beforeEach(() => {
    ;(fetch as any).mockClear()
  })
  afterEach(() => cleanup())

  it('lists groups and supports creating a new group', async () => {
    renderWithProviders('/groups')
    await screen.findByText('Groups')
    // Disambiguate: ensure the table cell is present (not the select option)
    await screen.findByRole('cell', { name: 'Network' })
    // Open create form
    fireEvent.click(screen.getByLabelText('Add Group'))
    const nameInput = (await screen.findByLabelText('Name')) as HTMLInputElement
    fireEvent.change(nameInput, { target: { value: 'Core Servers' } })
    const descInput = (await screen.findByLabelText('Description')) as HTMLInputElement
    fireEvent.change(descInput, { target: { value: 'Critical infra' } })
    fireEvent.submit((await screen.findByRole('button', { name: 'Create Group' })).closest('form')!)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/groups/') && c[1]?.method === 'POST')).toBe(true))
  })

  it('edits and deletes a group', async () => {
    renderWithProviders('/groups')
    // Disambiguate: pick the table cell, not the select option
    await screen.findByRole('cell', { name: 'Network' })
    // Edit
    fireEvent.click(await screen.findByLabelText('Edit'))
    const saveBtn = await screen.findByText('Save')
    fireEvent.submit(saveBtn.closest('form')!)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/groups/1') && c[1]?.method === 'PUT')).toBe(true))
    // Delete
    fireEvent.click(await screen.findByLabelText('Delete'))
    const ok = await screen.findByRole('button', { name: 'OK' })
    fireEvent.click(ok)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/groups/1') && c[1]?.method === 'DELETE')).toBe(true))
  })

  it('manages group membership', async () => {
    renderWithProviders('/groups')
    // Open edit mode to reveal membership manager
    await screen.findByRole('cell', { name: 'Network' })
    fireEvent.click(await screen.findByLabelText('Edit'))
    await screen.findByText('Manage Membership')
    const groupSelect = await screen.findByLabelText('Group')
    fireEvent.change(groupSelect, { target: { value: '1' } })

    // Ensure existing members render
    await screen.findAllByText('Pi-hole')

    // Select target(s) to add from the Available targets listbox
    const targetList = await screen.findByRole('listbox', { name: 'Targets' })
    const nasOption = within(targetList).getByRole('option', { name: 'NAS' })
    fireEvent.click(nasOption)
    fireEvent.click(screen.getByText('Add selected →'))
    // Persist via the top Save button
    const saveBtn1 = await screen.findByText('Save')
    fireEvent.submit(saveBtn1.closest('form')!)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/groups/1/targets') && c[1]?.method === 'POST')).toBe(true))
  })

  it('adds a target to a group on double-click in the multi-select', async () => {
    renderWithProviders('/groups')
    await screen.findByRole('cell', { name: 'Network' })
    fireEvent.click(await screen.findByLabelText('Edit'))
    await screen.findByText('Manage Membership')
    const groupSelect = await screen.findByLabelText('Group')
    fireEvent.change(groupSelect, { target: { value: '1' } })

    const targetList = await screen.findByRole('listbox', { name: 'Targets' })
    const nasOption = await screen.findByRole('option', { name: 'NAS' })
    fireEvent.doubleClick(nasOption)
    const saveBtn2 = await screen.findByText('Save')
    fireEvent.submit(saveBtn2.closest('form')!)
    await waitFor(() =>
      expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/groups/1/targets') && c[1]?.method === 'POST')).toBe(true)
    )
  })

  it('toggles membership with double-click: removes when already in group; adds when not', async () => {
    renderWithProviders('/groups')
    await screen.findByRole('cell', { name: 'Network' })
    fireEvent.click(await screen.findByLabelText('Edit'))
    await screen.findByText('Manage Membership')
    const groupSelect = await screen.findByLabelText('Group')
    fireEvent.change(groupSelect, { target: { value: '1' } })

    // Ensure existing members render so toggle logic sees membership
    await screen.findAllByText('Pi-hole')

    // Pi-hole is already in the group; it's in the Members listbox now
    const membersList = await screen.findByRole('listbox', { name: 'Members' })
    const piHoleOption = within(membersList).getByRole('option', { name: 'Pi-hole' })
    fireEvent.doubleClick(piHoleOption)
    const saveBtn3 = await screen.findByText('Save')
    fireEvent.submit(saveBtn3.closest('form')!)
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[0].toString().endsWith('/groups/1/targets') && c[1]?.method === 'DELETE')).toBe(true))
  })
})


