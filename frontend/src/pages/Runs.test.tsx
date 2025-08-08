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
    await screen.findByText('Runs')
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

    console.log('initial start value:', startInput.value)
    console.log('initial end value:', endInput.value)

    // Simulate a date selection (many browsers inject a time; we ignore it and default)
    fireEvent.change(startInput, { target: { value: '2024-01-02T12:34' } })
    fireEvent.change(endInput, { target: { value: '2024-01-03T12:34' } })

    console.log('after change start value:', startInput.value)
    console.log('after change end value:', endInput.value)

    await waitFor(() => {
      console.log('asserting start value:', startInput.value)
      console.log('asserting end value:', endInput.value)
      expect(startInput.value).toBe('2024-01-02T00:00')
      expect(endInput.value).toBe('2024-01-03T23:59')
    })
  })
})


