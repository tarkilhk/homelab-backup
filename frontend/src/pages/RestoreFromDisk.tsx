import { useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Target } from '../api/client'
import { formatLocalDateTime } from '../lib/dates'
import AppCard from '../components/ui/AppCard'
import { X, RefreshCw, AlertCircle, CheckCircle2, Info } from 'lucide-react'
import { toast } from 'sonner'

type BackupFromDisk = {
  artifact_path: string
  target_slug: string | null
  date: string | null
  plugin_name: string | null
  file_size: number
  modified_at: string
  metadata_source: 'sidecar' | 'inferred'
}

const formatBytes = (value: number): string => {
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let num = Math.max(0, value)
  let unitIdx = 0
  while (num >= 1024 && unitIdx < units.length - 1) {
    num /= 1024
    unitIdx += 1
  }
  const rounded = num >= 100 ? Math.round(num) : Math.round(num * 10) / 10
  return `${rounded} ${units[unitIdx]}`
}

export default function RestoreFromDiskPage() {
  const queryClient = useQueryClient()
  const [selectedBackup, setSelectedBackup] = useState<BackupFromDisk | null>(null)
  const [selectedDestination, setSelectedDestination] = useState<number | ''>('')
  const [selectedPlugin, setSelectedPlugin] = useState<string | ''>('')
  const [isRestoring, setIsRestoring] = useState(false)
  const [restoreError, setRestoreError] = useState<string | null>(null)

  const { data: backups, isLoading, error, refetch } = useQuery({
    queryKey: ['backups-from-disk'],
    queryFn: api.listBackupsFromDisk,
  })

  const { data: targets } = useQuery({ queryKey: ['targets'], queryFn: api.listTargets })
  const { data: plugins } = useQuery({ queryKey: ['plugins'], queryFn: api.listPlugins })

  // Filter eligible targets based on plugin
  const eligibleTargets = useMemo(() => {
    if (!selectedBackup) return []
    
    const pluginName = selectedPlugin || selectedBackup.plugin_name
    if (!pluginName) return []
    
    return (targets ?? []).filter((t) => t.plugin_name === pluginName)
  }, [selectedBackup, selectedPlugin, targets])

  const resetRestoreState = () => {
    setSelectedBackup(null)
    setSelectedDestination('')
    setSelectedPlugin('')
    setRestoreError(null)
  }

  const handleConfirmRestore = async () => {
    if (!selectedBackup || typeof selectedDestination !== 'number') return
    
    setIsRestoring(true)
    setRestoreError(null)
    
    try {
      await api.restoreTargetRun({
        artifact_path: selectedBackup.artifact_path,
        destination_target_id: selectedDestination,
        triggered_by: 'restore_from_disk',
      })
      
      toast.success('Restore triggered successfully')
      resetRestoreState()
      // Refresh the backups list (restored backup should disappear if it creates a record)
      await refetch()
      queryClient.invalidateQueries({ queryKey: ['runs'], exact: false })
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      setRestoreError(message)
      toast.error(`Restore failed: ${message}`)
    } finally {
      setIsRestoring(false)
    }
  }

  if (isLoading) {
    return (
      <AppCard>
        <div className="text-center py-8">
          <RefreshCw className="h-8 w-8 animate-spin mx-auto text-gray-400 mb-4" />
          <div className="text-sm text-gray-600">Scanning backup directory...</div>
        </div>
      </AppCard>
    )
  }

  if (error) {
    return (
      <AppCard>
        <div className="text-center py-8">
          <AlertCircle className="h-8 w-8 mx-auto text-red-500 mb-4" />
          <div className="text-sm text-red-600">Error scanning backups: {String(error)}</div>
          <button
            onClick={() => refetch()}
            className="mt-4 px-4 py-2 bg-[hsl(var(--accent))] text-white rounded-md hover:opacity-90"
          >
            Retry
          </button>
        </div>
      </AppCard>
    )
  }

  if (!backups || backups.length === 0) {
    return (
      <AppCard>
        <div className="text-center py-8">
          <Info className="h-8 w-8 mx-auto text-gray-400 mb-4" />
          <div className="text-sm text-gray-600">No backup files found on disk.</div>
          <div className="text-xs text-gray-500 mt-2">
            Backup artifacts are typically stored in <code className="bg-muted px-1 rounded">/backups/&lt;target_slug&gt;/&lt;YYYY-MM-DD&gt;/</code>
          </div>
        </div>
      </AppCard>
    )
  }

  return (
    <div className="space-y-4">
      <AppCard>
        <div className="flex items-center justify-between mb-4">
          <div>
            <h1 className="text-2xl font-bold">Restore from Disk</h1>
            <p className="text-sm text-gray-600 mt-1">
              Discover and restore backup artifacts found on disk. These backups may not have corresponding database records.
            </p>
          </div>
          <button
            onClick={() => refetch()}
            className="px-4 py-2 bg-[hsl(var(--accent))] text-white rounded-md hover:opacity-90 flex items-center gap-2"
          >
            <RefreshCw className="h-4 w-4" />
            Refresh
          </button>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b">
                <th className="text-left px-4 py-2">Artifact Path</th>
                <th className="text-left px-4 py-2">Target Slug</th>
                <th className="text-left px-4 py-2">Date</th>
                <th className="text-left px-4 py-2">Plugin</th>
                <th className="text-left px-4 py-2">Size</th>
                <th className="text-left px-4 py-2">Modified</th>
                <th className="text-left px-4 py-2">Metadata</th>
                <th className="text-left px-4 py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {backups.map((backup) => (
                <tr key={backup.artifact_path} className="border-b hover:bg-muted/30">
                  <td className="px-4 py-2 font-mono text-xs break-all max-w-md">{backup.artifact_path}</td>
                  <td className="px-4 py-2">{backup.target_slug || '—'}</td>
                  <td className="px-4 py-2">{backup.date || '—'}</td>
                  <td className="px-4 py-2">
                    {backup.plugin_name ? (
                      <span className="inline-flex items-center px-2 py-1 rounded bg-[hsl(var(--accent)/.1)] text-xs">
                        {backup.plugin_name}
                      </span>
                    ) : (
                      <span className="text-gray-400 italic">Unknown</span>
                    )}
                  </td>
                  <td className="px-4 py-2">{formatBytes(backup.file_size)}</td>
                  <td className="px-4 py-2">{formatLocalDateTime(backup.modified_at)}</td>
                  <td className="px-4 py-2">
                    {backup.metadata_source === 'sidecar' ? (
                      <span className="inline-flex items-center gap-1 text-green-600">
                        <CheckCircle2 className="h-3 w-3" />
                        <span className="text-xs">Sidecar</span>
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-amber-600">
                        <AlertCircle className="h-3 w-3" />
                        <span className="text-xs">Inferred</span>
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() => {
                        setSelectedBackup(backup)
                        setSelectedDestination('')
                        setSelectedPlugin(backup.plugin_name || '')
                        setRestoreError(null)
                      }}
                      className="text-xs underline text-[hsl(var(--accent))] hover:opacity-80"
                    >
                      Restore
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AppCard>

      {selectedBackup && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={resetRestoreState}>
          <div className="bg-background border rounded-md shadow-xl max-w-2xl w-full mx-4" onClick={(e) => e.stopPropagation()}>
            <div className="p-4 border-b flex items-center">
              <div className="font-semibold">Restore Backup from Disk</div>
              <button
                aria-label="Close restore dialog"
                className="ml-auto text-sm cursor-pointer"
                onClick={resetRestoreState}
              >
                <X className="h-5 w-5 text-red-500" />
              </button>
            </div>
            <div className="p-4 space-y-4">
              <div className="space-y-2">
                <div><span className="font-semibold">Artifact path:</span> <code className="text-xs bg-muted px-1 rounded">{selectedBackup.artifact_path}</code></div>
                <div><span className="font-semibold">Target slug:</span> {selectedBackup.target_slug || '—'}</div>
                <div><span className="font-semibold">Date:</span> {selectedBackup.date || '—'}</div>
                <div><span className="font-semibold">Size:</span> {formatBytes(selectedBackup.file_size)}</div>
                <div><span className="font-semibold">Modified:</span> {formatLocalDateTime(selectedBackup.modified_at)}</div>
                <div>
                  <span className="font-semibold">Metadata source:</span>{' '}
                  {selectedBackup.metadata_source === 'sidecar' ? (
                    <span className="text-green-600">Sidecar (trusted)</span>
                  ) : (
                    <span className="text-amber-600">Inferred (may need verification)</span>
                  )}
                </div>
              </div>

              {!selectedBackup.plugin_name && (
                <div className="space-y-2">
                  <label className="block text-sm font-semibold">Select Plugin</label>
                  <select
                    value={selectedPlugin}
                    onChange={(e) => {
                      setSelectedPlugin(e.target.value)
                      setSelectedDestination('')
                    }}
                    className="w-full px-3 py-2 border rounded-md"
                  >
                    <option value="">— Select plugin —</option>
                    {plugins?.map((p) => (
                      <option key={p.key} value={p.key}>
                        {p.name || p.key}
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-gray-600">
                    Plugin could not be determined from filename. Please select the plugin that created this backup.
                  </p>
                </div>
              )}

              {eligibleTargets.length === 0 && (selectedBackup.plugin_name || selectedPlugin) ? (
                <div className="p-3 bg-amber-50 border border-amber-200 rounded-md">
                  <div className="text-sm text-amber-800">
                    No targets found using the <strong>{selectedBackup.plugin_name || selectedPlugin}</strong> plugin.
                    Create a target with this plugin first.
                  </div>
                </div>
              ) : eligibleTargets.length > 0 ? (
                <div className="space-y-2">
                  <label className="block text-sm font-semibold">Select Destination Target</label>
                  <select
                    value={selectedDestination}
                    onChange={(e) => {
                      setSelectedDestination(Number(e.target.value))
                      setRestoreError(null)
                    }}
                    className="w-full px-3 py-2 border rounded-md"
                  >
                    <option value="">— Select target —</option>
                    {eligibleTargets.map((t: Target) => (
                      <option key={t.id} value={t.id}>
                        {t.name} ({t.plugin_name})
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}

              {restoreError && <div className="text-sm text-red-600">{restoreError}</div>}

              <div className="flex gap-2 justify-end pt-4 border-t">
                <button
                  onClick={resetRestoreState}
                  className="px-4 py-2 border rounded-md hover:bg-muted"
                  disabled={isRestoring}
                >
                  Cancel
                </button>
                <button
                  onClick={handleConfirmRestore}
                  disabled={typeof selectedDestination !== 'number' || isRestoring || eligibleTargets.length === 0 || (!selectedBackup.plugin_name && !selectedPlugin)}
                  className="px-4 py-2 bg-[hsl(var(--accent))] text-white rounded-md hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {isRestoring ? 'Restoring…' : 'Confirm Restore'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}


