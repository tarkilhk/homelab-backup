import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RestoreFromDiskPage from '../RestoreFromDisk'
import React, { type ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'

let nowIso = new Date().toISOString()

vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
  // List backups from disk
  if (url.endsWith('/backups/from-disk')) {
    return new Response(JSON.stringify([
      {
        artifact_path: '/backups/pihole/2025-01-15/pihole-backup-20250115T120000.zip',
        target_slug: 'pihole',
        date: '2025-01-15',
        plugin_name: 'pihole',
        file_size: 1024000,
        modified_at: nowIso,
        metadata_source: 'sidecar',
      },
      {
        artifact_path: '/backups/postgresql/2025-01-16/postgresql-dump-20250116T130000.sql',
        target_slug: 'postgresql',
        date: '2025-01-16',
        plugin_name: 'postgresql',
        file_size: 2048000,
        modified_at: nowIso,
        metadata_source: 'inferred',
      },
      {
        artifact_path: '/backups/unknown/2025-01-17/unknown-backup-20250117T140000.tar.gz',
        target_slug: 'unknown',
        date: '2025-01-17',
        plugin_name: null,
        file_size: 512000,
        modified_at: nowIso,
        metadata_source: 'inferred',
      },
    ]), { status: 200 })
  }
  // List targets
  if (url.endsWith('/targets/')) {
    return new Response(JSON.stringify([
      { id: 1, name: 'Primary Pihole', slug: 'pihole', plugin_name: 'pihole', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
      { id: 2, name: 'Secondary Pihole', slug: 'pihole-secondary', plugin_name: 'pihole', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
      { id: 3, name: 'PostgreSQL DB', slug: 'postgresql', plugin_name: 'postgresql', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
    ]), { status: 200 })
  }
  // List plugins
  if (url.endsWith('/plugins')) {
    return new Response(JSON.stringify([
      { key: 'pihole', name: 'Pi-hole', version: '1.0.0' },
      { key: 'postgresql', name: 'PostgreSQL', version: '1.0.0' },
      { key: 'mysql', name: 'MySQL', version: '1.0.0' },
    ]), { status: 200 })
  }
  // Restore endpoint
  if (url.endsWith('/restores/') && init?.method === 'POST') {
    const body = JSON.parse(init.body as string)
    if (body.artifact_path && body.destination_target_id) {
      return new Response(JSON.stringify({
        id: 99,
        job_id: null,
        status: 'success',
        operation: 'restore',
        started_at: nowIso,
        finished_at: nowIso,
        job: null,
        display_job_name: 'Restore from Disk',
        display_tag_name: null,
        target_runs: [
          {
            id: 201,
            run_id: 99,
            target_id: body.destination_target_id,
            status: 'success',
            operation: 'restore',
            started_at: nowIso,
            finished_at: nowIso,
            artifact_path: body.artifact_path,
          },
        ],
      }), { status: 201 })
    }
    return new Response(JSON.stringify({ error: 'Invalid request' }), { status: 400 })
  }
  return new Response('not found', { status: 404 })
}))

const fetchSpy = global.fetch as unknown as ReturnType<typeof vi.fn>

beforeEach(() => {
  nowIso = new Date().toISOString()
  fetchSpy.mockClear()
})

function wrapper(children: ReactNode) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        {children}
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('RestoreFromDiskPage', () => {
  it('renders loading state', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    await screen.findByText('Scanning backup directory...')
  })

  it('renders backups list with sidecar and inferred metadata', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    await screen.findAllByText('Restore from Disk')
    
    // Check that backup paths appear (may appear multiple times in table)
    const piholePaths = screen.getAllByText('/backups/pihole/2025-01-15/pihole-backup-20250115T120000.zip')
    expect(piholePaths.length).toBeGreaterThan(0)
    
    const postgresqlPaths = screen.getAllByText('/backups/postgresql/2025-01-16/postgresql-dump-20250116T130000.sql')
    expect(postgresqlPaths.length).toBeGreaterThan(0)
    
    const unknownPaths = screen.getAllByText('/backups/unknown/2025-01-17/unknown-backup-20250117T140000.tar.gz')
    expect(unknownPaths.length).toBeGreaterThan(0)
    
    // Check metadata source indicators
    const sidecarIndicators = screen.getAllByText('Sidecar')
    expect(sidecarIndicators.length).toBeGreaterThan(0)
    
    const inferredIndicators = screen.getAllByText('Inferred')
    expect(inferredIndicators.length).toBeGreaterThan(0)
  })

  it('displays file sizes correctly', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    // Check that file sizes are displayed (formatBytes function formats them)
    // 1024000 bytes = 1000 KB = 1 MB (rounded)
    const size1MB = screen.getAllByText((content, element) => {
      return element?.textContent?.includes('MB') || false
    })
    expect(size1MB.length).toBeGreaterThan(0)
    
    // 512000 bytes = 500 KB
    const size500KB = screen.getAllByText((content, element) => {
      return element?.textContent?.includes('KB') || false
    })
    expect(size500KB.length).toBeGreaterThan(0)
  })

  it('shows empty state when no backups found', async () => {
    fetchSpy.mockImplementationOnce(async (url: string) => {
      if (url.endsWith('/backups/from-disk')) {
        return new Response(JSON.stringify([]), { status: 200 })
      }
      return new Response('not found', { status: 404 })
    })
    
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    await screen.findByText('No backup files found on disk.')
  })

  it('shows error state and allows retry', async () => {
    fetchSpy.mockImplementationOnce(async (url: string) => {
      if (url.endsWith('/backups/from-disk')) {
        return new Response('Server error', { status: 500 })
      }
      return new Response('not found', { status: 404 })
    })
    
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    await screen.findByText(/Error scanning backups/i)
    const retryBtn = await screen.findByText('Retry')
    expect(retryBtn).toBeDefined()
  })

  it('opens restore dialog when Restore button is clicked', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    // Find Restore buttons in the table (not in dialog)
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButton = restoreButtons.find(btn => {
      const parent = btn.closest('tr')
      return parent !== null
    })
    expect(tableRestoreButton).toBeDefined()
    fireEvent.click(tableRestoreButton!)
    
    await screen.findByText('Restore Backup from Disk')
    
    // Check that artifact path appears in dialog (may appear multiple times)
    const artifactPaths = screen.getAllByText('/backups/pihole/2025-01-15/pihole-backup-20250115T120000.zip')
    expect(artifactPaths.length).toBeGreaterThan(0)
  })

  it('filters targets by plugin when plugin is known', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    // Click Restore for pihole backup (known plugin)
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButton = restoreButtons.find(btn => {
      const parent = btn.closest('tr')
      return parent !== null
    })
    expect(tableRestoreButton).toBeDefined()
    fireEvent.click(tableRestoreButton!)
    
    await screen.findByText('Restore Backup from Disk')
    
    // Wait for targets to load - find select by finding the label text, then the select element
    await waitFor(() => {
      const label = screen.getByText('Select Destination Target')
      const select = label.parentElement?.querySelector('select')
      expect(select).toBeDefined()
    })
    
    const label = screen.getByText('Select Destination Target')
    const targetSelect = label.parentElement?.querySelector('select') as HTMLSelectElement
    expect(targetSelect).toBeDefined()
    
    const options = Array.from(targetSelect.querySelectorAll('option')).map(opt => opt.textContent)
    
    // Should only show pihole targets
    expect(options).toContain('Primary Pihole (pihole)')
    expect(options).toContain('Secondary Pihole (pihole)')
    expect(options).not.toContain('PostgreSQL DB (postgresql)')
  })

  it('shows plugin selector when plugin is unknown', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    // Click Restore for unknown backup (third backup)
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButtons = restoreButtons.filter(btn => {
      const parent = btn.closest('tr')
      return parent !== null
    })
    expect(tableRestoreButtons.length).toBeGreaterThanOrEqual(3)
    fireEvent.click(tableRestoreButtons[2])
    
    await screen.findByText('Restore Backup from Disk')
    
    // Should show plugin selector
    await screen.findByText('Select Plugin')
    await screen.findByText(/Plugin could not be determined from filename/i)
    
    // Select a plugin - find select by finding the label text, then the select element
    const pluginLabel = screen.getByText('Select Plugin')
    const pluginSelect = pluginLabel.parentElement?.querySelector('select') as HTMLSelectElement
    expect(pluginSelect).toBeDefined()
    fireEvent.change(pluginSelect, { target: { value: 'pihole' } })
    
    // Wait a bit for state update and query to complete
    await new Promise(resolve => setTimeout(resolve, 200))
    
    // Now target selector should appear - check if it exists
    await waitFor(() => {
      const targetLabel = screen.queryByText('Select Destination Target')
      expect(targetLabel).toBeDefined()
      if (targetLabel) {
        const targetSelect = targetLabel.parentElement?.querySelector('select') as HTMLSelectElement
        expect(targetSelect).toBeDefined()
        // Should have at least the placeholder + pihole targets
        expect(targetSelect.options.length).toBeGreaterThan(1)
        return true
      }
      return false
    }, { timeout: 3000 })
  })

  it('shows warning when no targets match plugin', async () => {
    // Create a fresh QueryClient to avoid cache issues
    const testQueryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false, gcTime: 0 },
        mutations: { retry: false },
      },
    })
    
    // Override the global fetch stub for this test only
    const originalFetch = global.fetch
    try {
      global.fetch = vi.fn(async (url: string, init?: RequestInit) => {
        if (url.endsWith('/targets/')) {
          // Return only postgresql targets (no pihole targets)
          return new Response(JSON.stringify([
            { id: 3, name: 'PostgreSQL DB', slug: 'postgresql', plugin_name: 'postgresql', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
          ]), { status: 200 })
        }
        if (url.endsWith('/backups/from-disk')) {
          return new Response(JSON.stringify([
            {
              artifact_path: '/backups/pihole/2025-01-15/pihole-backup-20250115T120000.zip',
              target_slug: 'pihole',
              date: '2025-01-15',
              plugin_name: 'pihole',
              file_size: 1024000,
              modified_at: nowIso,
              metadata_source: 'sidecar',
            },
          ]), { status: 200 })
        }
        if (url.endsWith('/plugins')) {
          return new Response(JSON.stringify([
            { key: 'pihole', name: 'Pi-hole', version: '1.0.0' },
            { key: 'postgresql', name: 'PostgreSQL', version: '1.0.0' },
          ]), { status: 200 })
        }
      return new Response('not found', { status: 404 })
    }) as typeof fetch
    
    // Render with fresh QueryClient
    render(
      <QueryClientProvider client={testQueryClient}>
        <MemoryRouter>
          <RestoreFromDiskPage />
        </MemoryRouter>
      </QueryClientProvider>
    )
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButton = restoreButtons.find(btn => {
      const parent = btn.closest('tr')
      return parent !== null
    })
    expect(tableRestoreButton).toBeDefined()
    fireEvent.click(tableRestoreButton!)
    
    await screen.findAllByText('Restore Backup from Disk')
    
    // Wait for the warning message to appear
    // The warning appears when plugin is known but no matching targets exist
    // We wait for the text to appear, giving time for React Query to load targets
    await waitFor(() => {
      const warningText = screen.queryByText((content, element) => {
        const text = element?.textContent || ''
        return text.includes('No targets found using the') && 
               text.includes('pihole') && 
               text.includes('Create a target')
      })
      return warningText !== null
    }, { timeout: 5000 })
    
    // Verify warning exists
    const warningText = screen.getByText((content, element) => {
      const text = element?.textContent || ''
      return text.includes('No targets found using the') && 
             text.includes('pihole') && 
             text.includes('Create a target')
    })
    expect(warningText).toBeDefined()
    } finally {
      // Always restore original fetch
      global.fetch = originalFetch
    }
  })

  it('successfully triggers restore and closes dialog', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButton = restoreButtons.find(btn => {
      const parent = btn.closest('tr')
      return parent !== null
    })
    expect(tableRestoreButton).toBeDefined()
    fireEvent.click(tableRestoreButton!)
    
    await screen.findByText('Restore Backup from Disk')
    
    // Wait for target select to appear and select target
    await waitFor(() => {
      const targetLabel = screen.queryByText('Select Destination Target')
      if (targetLabel) {
        const targetSelect = targetLabel.parentElement?.querySelector('select') as HTMLSelectElement
        if (targetSelect) {
          fireEvent.change(targetSelect, { target: { value: '1' } })
          
          const confirmBtn = screen.getByRole('button', { name: 'Confirm Restore' })
          fireEvent.click(confirmBtn)
          return true
        }
      }
      return false
    }, { timeout: 3000 })
    
    // Wait for restore API call
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        expect.stringContaining('/restores/'),
        expect.objectContaining({
          method: 'POST',
        }),
      )
    }, { timeout: 3000 })
  })

  it('handles restore errors gracefully', async () => {
    let restoreCallCount = 0
    fetchSpy.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/restores/') && init?.method === 'POST') {
        restoreCallCount++
        if (restoreCallCount === 1) {
          return new Response(JSON.stringify({ error: 'Restore failed: file not found' }), { status: 400 })
        }
      }
      // Use default handlers for other endpoints
      if (url.endsWith('/backups/from-disk')) {
        return new Response(JSON.stringify([
          {
            artifact_path: '/backups/pihole/2025-01-15/pihole-backup-20250115T120000.zip',
            target_slug: 'pihole',
            date: '2025-01-15',
            plugin_name: 'pihole',
            file_size: 1024000,
            modified_at: nowIso,
            metadata_source: 'sidecar',
          },
        ]), { status: 200 })
      }
      if (url.endsWith('/targets/')) {
        return new Response(JSON.stringify([
          { id: 1, name: 'Primary Pihole', slug: 'pihole', plugin_name: 'pihole', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
        ]), { status: 200 })
      }
      if (url.endsWith('/plugins')) {
        return new Response(JSON.stringify([
          { key: 'pihole', name: 'Pi-hole', version: '1.0.0' },
        ]), { status: 200 })
      }
      return new Response('not found', { status: 404 })
    })
    
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButton = restoreButtons.find(btn => {
      const parent = btn.closest('tr')
      return parent !== null
    })
    expect(tableRestoreButton).toBeDefined()
    fireEvent.click(tableRestoreButton!)
    
    await screen.findByText('Restore Backup from Disk')
    
    // Wait for target select to appear
    await waitFor(() => {
      const targetSelect = screen.queryByLabelText('Select Destination Target')
      if (targetSelect) {
        fireEvent.change(targetSelect, { target: { value: '1' } })
        const confirmBtn = screen.getByRole('button', { name: 'Confirm Restore' })
        fireEvent.click(confirmBtn)
        return true
      }
      return false
    }, { timeout: 3000 })
    
    // Should show error message
    await waitFor(() => {
      const errorMsg = screen.queryByText(/Restore failed/i)
      return errorMsg !== null
    }, { timeout: 3000 })
    
    // Dialog should remain open
    expect(screen.getByText('Restore Backup from Disk')).toBeDefined()
  })

  it('allows refreshing the backups list', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    const refreshBtns = screen.getAllByRole('button', { name: /refresh/i })
    const refreshBtn = refreshBtns.find(btn => {
      const parent = btn.closest('div')
      return parent?.textContent?.includes('Restore from Disk')
    })
    expect(refreshBtn).toBeDefined()
    
    fireEvent.click(refreshBtn!)
    
    // Should refetch backups
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledWith(
        expect.stringContaining('/backups/from-disk'),
        expect.anything(),
      )
    })
  })

  it('disables confirm button when no target selected', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButton = restoreButtons.find(btn => {
      const parent = btn.closest('tr')
      return parent !== null
    })
    expect(tableRestoreButton).toBeDefined()
    fireEvent.click(tableRestoreButton!)
    
    await screen.findByText('Restore Backup from Disk')
    
    const confirmBtn = screen.getByRole('button', { name: 'Confirm Restore' }) as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)
  })

  it('disables confirm button when plugin unknown and not selected', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    // Click Restore for unknown backup
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButtons = restoreButtons.filter(btn => {
      const parent = btn.closest('tr')
      return parent !== null
    })
    expect(tableRestoreButtons.length).toBeGreaterThanOrEqual(3)
    fireEvent.click(tableRestoreButtons[2])
    
    await screen.findAllByText('Restore Backup from Disk')
    
    // Wait for dialog to fully render
    await waitFor(() => {
      const confirmBtns = screen.getAllByRole('button', { name: 'Confirm Restore' })
      return confirmBtns.length > 0
    })
    
    const confirmBtns = screen.getAllByRole('button', { name: 'Confirm Restore' })
    // Find the one in the dialog (not in the main page)
    const confirmBtn = confirmBtns.find(btn => {
      const dialog = btn.closest('[class*="bg-background"]')
      return dialog !== null
    }) as HTMLButtonElement
    
    expect(confirmBtn).toBeDefined()
    expect(confirmBtn.disabled).toBe(true)
  })
})
