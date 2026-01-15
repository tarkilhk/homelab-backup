import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { formatLocalDateTimeShort } from '../lib/dates'
import AppCard from '../components/ui/AppCard'
import { CheckCircle2, AlertTriangle, Loader2 } from 'lucide-react'
import { cn } from '../lib/cn'

const formatShortDateTime = (dt?: string | null): string =>
  dt ? formatLocalDateTimeShort(dt) : '—'

const formatDuration = (started: string, finished: string | null): string => {
  if (!finished) return 'Running...'
  try {
    const start = new Date(started)
    const end = new Date(finished)
    const seconds = Math.floor((end.getTime() - start.getTime()) / 1000)
    if (seconds < 60) return `${seconds}s`
    const minutes = Math.floor(seconds / 60)
    if (minutes < 60) return `${minutes}m ${seconds % 60}s`
    const hours = Math.floor(minutes / 60)
    return `${hours}h ${minutes % 60}m`
  } catch {
    return '—'
  }
}

function StatusBadge({ status }: { status: string }) {
  const baseClasses = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium'
  switch (status) {
    case 'success':
      return (
        <span className={cn(baseClasses, 'bg-green-100 text-green-700 border border-green-200')}>
          <CheckCircle2 className="h-3.5 w-3.5" />
          Success
        </span>
      )
    case 'failed':
      return (
        <span className={cn(baseClasses, 'bg-red-100 text-red-700 border border-red-200')}>
          <AlertTriangle className="h-3.5 w-3.5" />
          Failed
        </span>
      )
    case 'running':
      return (
        <span className={cn(baseClasses, 'bg-yellow-100 text-yellow-700 border border-yellow-200')}>
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          Running
        </span>
      )
    default:
      return (
        <span className={cn(baseClasses, 'bg-gray-100 text-gray-700 border border-gray-200')}>
          {status}
        </span>
      )
  }
}

export default function MaintenanceJobsPage() {
  const { data: runs, isLoading, error } = useQuery({
    queryKey: ['maintenance-runs'],
    queryFn: () => api.listMaintenanceRuns(50),
  })

  if (isLoading) {
    return (
      <div className="space-y-10">
        <div className="relative overflow-hidden rounded-2xl p-6 border surface-card">
          <h1 className="text-2xl font-semibold">Maintenance Jobs</h1>
          <p className="text-sm text-muted-foreground">View status and logs of maintenance operations</p>
        </div>
        <AppCard title="Loading..." description="Fetching maintenance runs...">
          <div className="text-sm text-muted-foreground">Please wait...</div>
        </AppCard>
      </div>
    )
  }

  if (error) {
    return (
      <div className="space-y-10">
        <div className="relative overflow-hidden rounded-2xl p-6 border surface-card">
          <h1 className="text-2xl font-semibold">Maintenance Jobs</h1>
          <p className="text-sm text-muted-foreground">View status and logs of maintenance operations</p>
        </div>
        <AppCard title="Error" description="Failed to load maintenance runs">
          <div className="text-sm text-red-600">
            {(error as Error).message || 'An error occurred while loading maintenance runs'}
          </div>
        </AppCard>
      </div>
    )
  }

  if (!runs || runs.length === 0) {
    return (
      <div className="space-y-10">
        <div className="relative overflow-hidden rounded-2xl p-6 border surface-card">
          <h1 className="text-2xl font-semibold">Maintenance Jobs</h1>
          <p className="text-sm text-muted-foreground">View status and logs of maintenance operations</p>
        </div>
        <AppCard title="No Maintenance Runs" description="No maintenance jobs have run yet">
          <div className="text-sm text-muted-foreground">
            Maintenance runs will appear here once retention cleanup or other maintenance tasks have been executed.
          </div>
        </AppCard>
      </div>
    )
  }

  return (
    <div className="space-y-10">
      <div className="relative overflow-hidden rounded-2xl p-6 border surface-card">
        <h1 className="text-2xl font-semibold">Maintenance Jobs</h1>
        <p className="text-sm text-muted-foreground">View status and logs of maintenance operations</p>
      </div>

      <AppCard title="Maintenance Run History" description="Recent maintenance job executions">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left py-3 px-4 font-semibold">Job</th>
                <th className="text-left py-3 px-4 font-semibold">Started</th>
                <th className="text-left py-3 px-4 font-semibold">Finished</th>
                <th className="text-left py-3 px-4 font-semibold">Duration</th>
                <th className="text-left py-3 px-4 font-semibold">Status</th>
                <th className="text-left py-3 px-4 font-semibold">Summary</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.id} className="border-b border-border/50 hover:bg-muted/30">
                  <td className="py-3 px-4">
                    <div className="font-medium">{run.job?.name || 'Unknown Job'}</div>
                    <div className="text-xs text-muted-foreground">{run.job?.job_type || '—'}</div>
                  </td>
                  <td className="py-3 px-4 text-muted-foreground">
                    {formatShortDateTime(run.started_at)}
                  </td>
                  <td className="py-3 px-4 text-muted-foreground">
                    {formatShortDateTime(run.finished_at)}
                  </td>
                  <td className="py-3 px-4 text-muted-foreground">
                    {formatDuration(run.started_at, run.finished_at)}
                  </td>
                  <td className="py-3 px-4">
                    <StatusBadge status={run.status} />
                  </td>
                  <td className="py-3 px-4">
                    {run.result ? (
                      <div className="space-y-1">
                        {run.result.error ? (
                          <div className="text-xs text-red-600">{run.result.error}</div>
                        ) : (
                          <>
                            {run.result.targets_processed != null && (
                              <div className="text-xs">
                                <span className="text-muted-foreground">Targets:</span>{' '}
                                <span className="font-medium">{run.result.targets_processed}</span>
                              </div>
                            )}
                            {run.result.deleted_count != null && (
                              <div className="text-xs">
                                <span className="text-muted-foreground">Deleted:</span>{' '}
                                <span className="font-medium text-red-600">{run.result.deleted_count}</span>
                              </div>
                            )}
                            {run.result.kept_count != null && (
                              <div className="text-xs">
                                <span className="text-muted-foreground">Kept:</span>{' '}
                                <span className="font-medium text-green-600">{run.result.kept_count}</span>
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </AppCard>
    </div>
  )
}
