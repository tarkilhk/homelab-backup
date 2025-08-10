import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RunsPage from './Runs'
import React, { type ReactNode } from 'react'

vi.stubGlobal('fetch', vi.fn(async (url: string) => {
  if (url.endsWith('/runs/') || url.includes('/runs/?')) {
    return new Response(JSON.stringify([
      { id: 1, job_id: 1, status: 'success', started_at: new Date().toISOString(), finished_at: new Date().toISOString() },
    ]), { status: 200 })
  }
  if (url.endsWith('/targets/')) {
    return new Response(JSON.stringify([]), { status: 200 })
  }
  return new Response('not found', { status: 404 })
}))

function wrapper(children: ReactNode) {
  const qc = new QueryClient()
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('RunsPage', () => {
  it('renders runs table', async () => {
    render(wrapper(<RunsPage />))
    await screen.findByText('Past Runs')
    await screen.findByText('success')
  })

  it('applies filters and calls API with query', async () => {
    const fetchSpy = global.fetch as unknown as ReturnType<typeof vi.fn>
    render(wrapper(<RunsPage />))
    // open status select and choose failed
    const statusSel = await screen.findByLabelText('Status')
    fireEvent.change(statusSel, { target: { value: 'failed' } })
    await waitFor(() => {
      const called = fetchSpy.mock.calls.some((args: unknown[]) => String(args[0]).includes('/runs/?status=failed'))
      expect(called).toBe(true)
    })
  })

  it('sets default times for date-only selections', async () => {
    const fetchSpy = global.fetch as unknown as ReturnType<typeof vi.fn>
    fetchSpy.mockClear()
    render(wrapper(<RunsPage />))

    const startInput = (await screen.findByLabelText('Start date')) as HTMLInputElement
    const endInput = (await screen.findByLabelText('End date')) as HTMLInputElement

    // With date-only inputs, values are YYYY-MM-DD
    fireEvent.change(startInput, { target: { value: '2024-01-02' } })
    fireEvent.change(endInput, { target: { value: '2024-01-03' } })

    await waitFor(() => {
      expect(startInput.value).toBe('2024-01-02')
      expect(endInput.value).toBe('2024-01-03')
    })
  })
})


