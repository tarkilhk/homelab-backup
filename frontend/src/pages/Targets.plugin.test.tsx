import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import TargetsPage from './Targets'

vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
  if (url.endsWith('/plugins/')) {
    return new Response(JSON.stringify([
      { key: 'pihole', name: 'Pi-hole', version: '1.0.0' },
    ]), { status: 200 })
  }
  if (url.endsWith('/targets/')) {
    if (init?.method === 'POST') {
      const body = JSON.parse(init.body as string)
      if (body.plugin_name) {
        return new Response(JSON.stringify({
          id: 2,
          name: body.name,
          slug: body.slug,
          plugin_name: body.plugin_name,
          plugin_config_json: body.plugin_config_json,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }), { status: 201 })
      }
    }
    return new Response(JSON.stringify([]), { status: 200 })
  }
  return new Response('not found', { status: 404 })
}))

function wrapper(children: React.ReactNode) {
  const qc = new QueryClient()
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('TargetsPage plugin mode', () => {
  it('creates a target in plugin mode', async () => {
    render(wrapper(<TargetsPage />))
    await screen.findByText('Targets')

    fireEvent.change(await screen.findByLabelText('Plugin'), { target: { value: 'pihole' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Pi-hole' } })
    fireEvent.change(screen.getByLabelText('Slug'), { target: { value: 'pihole' } })
    fireEvent.change(screen.getByLabelText('Plugin Config JSON'), { target: { value: '{"foo":"bar"}' } })
    fireEvent.submit(screen.getByText('Create').closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
  })
})


