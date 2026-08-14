import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { ConfirmProvider } from '../../components/ConfirmProvider'
import OptionsPage from '../Options'

const policy = JSON.stringify({
  rules: [
    { unit: 'day', window: 7, keep: 1 },
    { unit: 'week', window: 4, keep: 1 },
    { unit: 'month', window: 6, keep: 1 },
  ],
})

function response(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <ConfirmProvider>
        <OptionsPage />
      </ConfirmProvider>
    </QueryClientProvider>,
  )
}

describe('Options retention cleanup', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input.toString()
      if (url.endsWith('/settings/') && !init?.method) {
        return response({
          id: 1,
          global_retention_policy_json: policy,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        })
      }
      if (url.endsWith('/settings/retention/preview') && init?.method === 'POST') {
        return response({
          targets_processed: 2,
          keep_count: 8,
          delete_count: 2,
          deleted_paths: ['/backups/a/old.tar', '/backups/b/old.tar'],
          failed_count: 0,
          failed_paths: [],
        })
      }
      if (url.endsWith('/settings/retention/run?confirmed=true') && init?.method === 'POST') {
        return response({
          targets_processed: 2,
          keep_count: 8,
          delete_count: 2,
          deleted_paths: ['/backups/a/old.tar', '/backups/b/old.tar'],
          failed_count: 0,
          failed_paths: [],
        })
      }
      return new Response('not found', { status: 404 })
    }))
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('previews cleanup and requires confirmation before deletion', async () => {
    renderPage()
    const runButton = await screen.findByRole('button', { name: 'Run Cleanup' })

    fireEvent.click(runButton)

    await screen.findByRole('dialog', { name: 'Confirm retention cleanup' })
    expect(screen.getByText(/permanently delete 2 backups/i)).toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringContaining('/settings/retention/run'),
      expect.anything(),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Delete 2 backups' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      '/api/v1/settings/retention/run?confirmed=true',
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('disables cleanup while displayed retention settings are unsaved', async () => {
    renderPage()
    const inputs = await screen.findAllByRole('spinbutton')
    const runButton = screen.getByRole('button', { name: 'Run Cleanup' })

    expect(runButton).toBeEnabled()
    fireEvent.change(inputs[0], { target: { value: '8' } })

    expect(runButton).toBeDisabled()
    expect(runButton).toHaveAttribute('title', 'Save retention settings before running cleanup')
  })
})
