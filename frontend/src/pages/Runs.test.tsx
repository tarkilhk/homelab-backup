import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RunsPage from './Runs'

vi.stubGlobal('fetch', vi.fn(async (url: string) => {
  if (url.endsWith('/runs/')) {
    return new Response(JSON.stringify([
      { id: 1, job_id: 1, status: 'success', started_at: new Date().toISOString(), finished_at: new Date().toISOString() },
    ]), { status: 200 })
  }
  return new Response('not found', { status: 404 })
}))

function wrapper(children: React.ReactNode) {
  const qc = new QueryClient()
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('RunsPage', () => {
  it('renders runs table', async () => {
    render(wrapper(<RunsPage />))
    await screen.findByText('Runs')
    await screen.findByText('success')
  })
})


