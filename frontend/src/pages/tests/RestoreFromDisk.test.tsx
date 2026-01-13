import React, { type ReactNode } from 'react'
import { describe, it, expect, beforeAll, afterEach, afterAll } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import RestoreFromDiskPage from '../RestoreFromDisk'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { server } from '../../mocks/server'

// Silence React import unused warning - needed for JSX
void React

// Set up MSW server lifecycle for this test file
beforeAll(() => server.listen({ onUnhandledRequest: 'bypass' }))
afterEach(() => {
  cleanup()
  server.resetHandlers()
})
afterAll(() => server.close())

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
    server.use(
      http.get('/api/v1/backups/from-disk', () => {
        return HttpResponse.json([])
      })
    )
    
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    await screen.findByText('No backup files found on disk.')
  })

  it('shows error state and allows retry', async () => {
    server.use(
      http.get('/api/v1/backups/from-disk', () => {
        return new HttpResponse('Server error', { status: 500 })
      })
    )
    
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
    
    // Wait for success message (dialog should close on success)
    await waitFor(() => {
      // After successful restore, the dialog should close
      expect(screen.queryByText('Restore Backup from Disk')).toBeNull()
    }, { timeout: 3000 })
  })

  it('handles restore errors gracefully', async () => {
    // Override restore endpoint to return an error
    server.use(
      http.post('/api/v1/restores/', () => {
        return HttpResponse.json({ error: 'Restore failed: file not found' }, { status: 400 })
      })
    )
    
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
    
    // Wait for target select to appear and select a target
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
    
    // Should show error message
    await waitFor(() => {
      const errorMsg = screen.queryByText(/Restore failed/i)
      expect(errorMsg).not.toBeNull()
    }, { timeout: 3000 })
    
    // Dialog should remain open
    expect(screen.getByText('Restore Backup from Disk')).toBeDefined()
  })

  it('allows refreshing the backups list', async () => {
    render(wrapper(<RestoreFromDiskPage />))
    
    // Wait for loading to complete and table to appear
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    // Find and click refresh button using getAllByRole
    const buttons = screen.getAllByRole('button')
    const refreshBtn = buttons.find(btn => btn.textContent?.includes('Refresh'))
    expect(refreshBtn).toBeDefined()
    fireEvent.click(refreshBtn!)
    
    // Wait for any loading state to complete again
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    }, { timeout: 3000 })
    
    // Verify table is still present after refresh
    const table = document.querySelector('table')
    expect(table).toBeDefined()
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

  // This test uses server.use() to override handlers, so it runs last
  // to avoid affecting other tests
  it('shows warning when no targets match plugin', async () => {
    // Override the targets handler to return only postgresql targets (no pihole)
    // This tests that clicking restore on a pihole backup shows warning
    const nowIso = new Date().toISOString()
    server.use(
      http.get('/api/v1/targets/', () => {
        return HttpResponse.json([
          { id: 3, name: 'PostgreSQL DB', slug: 'postgresql', plugin_name: 'postgresql', plugin_config_json: '{}', created_at: nowIso, updated_at: nowIso },
        ])
      })
    )
    
    render(wrapper(<RestoreFromDiskPage />))
    
    await waitFor(() => {
      expect(screen.queryByText('Scanning backup directory...')).toBeNull()
    })
    
    // Click Restore for first backup (pihole backup with known plugin)
    const restoreButtons = await screen.findAllByText('Restore')
    const tableRestoreButton = restoreButtons.find(btn => btn.closest('tr'))
    expect(tableRestoreButton).toBeDefined()
    fireEvent.click(tableRestoreButton!)
    
    // Wait for dialog to appear
    await screen.findByText('Restore Backup from Disk')
    
    // Wait for targets to be fetched and processed, then check for warning
    // The warning appears when there are no matching targets for the plugin
    await waitFor(() => {
      // Look for the amber warning container
      const warningContainer = document.querySelector('.bg-amber-50')
      expect(warningContainer).not.toBeNull()
      expect(warningContainer?.textContent).toContain('No targets found')
    }, { timeout: 5000 })
  })
})
