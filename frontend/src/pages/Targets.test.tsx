import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import TargetsPage from './Targets'

vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
  if (url.endsWith('/targets/')) {
    if (init?.method === 'POST') {
      return new Response(JSON.stringify({
        id: 1, name: 'N', slug: 'n', type: 't', config_json: '{}', created_at: new Date().toISOString(), updated_at: new Date().toISOString()
      }), { status: 201 })
    }
    return new Response(JSON.stringify([]), { status: 200 })
  }
  return new Response('not found', { status: 404 })
}))

function wrapper(children: React.ReactNode) {
  const qc = new QueryClient()
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('TargetsPage', () => {
  it('renders list and create form', async () => {
    render(wrapper(<TargetsPage />))
    await screen.findByText('Targets')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'N' } })
    fireEvent.change(screen.getByLabelText('Slug'), { target: { value: 'n' } })
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 't' } })
    fireEvent.submit(screen.getByText('Create').closest('form')!)
    await waitFor(() => expect(fetch).toHaveBeenCalled())
  })
})


