import { Activity, CalendarClock, Rocket, ShieldCheck, Puzzle } from 'lucide-react'
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import AppCard from '../components/ui/AppCard'
import { useNavigate } from 'react-router-dom'
import StatCard from '../components/StatCard'
import { api } from '../api/client'
import { formatLocalDateTime } from '../lib/dates'

const gapLabels = {
  not_scheduled: 'Not scheduled',
  never_succeeded: 'Never succeeded',
  scheduled_backup_missing: 'Scheduled backup missing',
} as const

function formatAge(ageSeconds: number): string {
  if (ageSeconds < 60) return `${Math.floor(ageSeconds)}s old`
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}m old`
  if (ageSeconds < 86400) return `${Math.floor(ageSeconds / 3600)}h old`
  return `${Math.floor(ageSeconds / 86400)}d old`
}

export default function DashboardPage() {
  const navigate = useNavigate()
  const { data: targets } = useQuery({ queryKey: ['targets'], queryFn: api.listTargets })
  const { data: jobs } = useQuery({ queryKey: ['jobs'], queryFn: api.listJobs })
  const { data: plugins } = useQuery({ queryKey: ['plugins'], queryFn: api.listPlugins })
  const {
    data: protection,
    isLoading: protectionLoading,
    isError: protectionError,
  } = useQuery({
    queryKey: ['protection'],
    queryFn: api.listProtection,
  })

  const { data: runs24 } = useQuery({
    queryKey: ['runs', 'last24h'],
    queryFn: () => {
      const now = new Date()
      const since = new Date(now.getTime() - 24 * 60 * 60 * 1000)
      return api.listRuns({ start_date: since.toISOString(), end_date: now.toISOString() })
    },
  })

  // Recent runs (latest N)
  const RECENT_RUNS_LIMIT = 5
  const { data: recentRuns } = useQuery({ queryKey: ['runs', 'recent'], queryFn: () => api.listRuns() })
  const topRecentRuns = useMemo(() => (recentRuns ?? []).slice(0, RECENT_RUNS_LIMIT), [recentRuns])

  // Upcoming jobs (next N from scheduler)
  const UPCOMING_JOBS_LIMIT = 5
  const { data: upcomingAll } = useQuery({ queryKey: ['jobs', 'upcoming'], queryFn: api.upcomingJobs })
  const upcoming = useMemo(() => (upcomingAll ?? []).slice(0, UPCOMING_JOBS_LIMIT), [upcomingAll])

  const metrics = useMemo(() => {
    const targetsCount = targets?.length ?? undefined
    const jobsCount = jobs?.length ?? undefined
    const runsCount = runs24?.length ?? undefined
    const successCount = runs24?.filter((r) => r.status === 'success').length ?? 0
    const totalRuns = runs24?.length ?? 0
    const successRate = totalRuns > 0 ? Math.round((successCount / totalRuns) * 100) : undefined
    const pluginsCount = plugins?.length ?? undefined
    return { targetsCount, jobsCount, runsCount, successRate, pluginsCount }
  }, [targets, jobs, runs24, plugins])

  return (
    <div className="space-y-6">
      <div className="relative overflow-hidden rounded-2xl p-6 border surface-card">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <p className="text-sm text-muted-foreground">Overview of your homelab backups.</p>
      </div>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <StatCard label="Plugins" value={metrics.pluginsCount ?? '—'} icon={Puzzle} />
        <StatCard label="Targets" value={metrics.targetsCount ?? '—'} icon={Rocket} onClick={() => navigate('/targets')} />
        <StatCard label="Jobs" value={metrics.jobsCount ?? '—'} icon={CalendarClock} onClick={() => navigate('/jobs')} />
        <StatCard label="Runs (24h)" value={metrics.runsCount ?? '—'} icon={Activity} onClick={() => navigate('/runs')} />
        <StatCard label="Success rate" value={metrics.successRate != null ? `${metrics.successRate}%` : '—'} icon={ShieldCheck} onClick={() => navigate('/runs')} />
      </div>
      <AppCard>
        <div className="mb-4">
          <h2 className="text-lg font-semibold">Backup protection</h2>
          <p className="text-sm text-muted-foreground">
            Schedule coverage and validated backup facts for every target.
          </p>
        </div>
        {protectionLoading ? (
          <div className="text-sm text-muted-foreground">Loading protection facts…</div>
        ) : protectionError ? (
          <div className="text-sm text-red-500">Protection facts could not be loaded.</div>
        ) : !protection?.length ? (
          <div className="text-sm text-muted-foreground">No targets configured.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1080px] text-left text-sm">
              <thead className="border-b text-xs uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="pb-2 pr-4 font-medium">Target</th>
                  <th className="pb-2 pr-4 font-medium">Schedule</th>
                  <th className="pb-2 pr-4 font-medium">Last attempt</th>
                  <th className="pb-2 pr-4 font-medium">Last validated backup</th>
                  <th className="pb-2 pr-4 font-medium">Next run</th>
                  <th className="pb-2 pr-4 text-center font-medium">Failures</th>
                  <th className="pb-2 font-medium">Problem</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {protection.map((target) => (
                  <tr key={target.target_id} className="align-top">
                    <td className="py-3 pr-4">
                      <button
                        type="button"
                        className="text-left font-medium hover:text-[hsl(var(--accent))]"
                        onClick={() => navigate('/targets')}
                      >
                        {target.target_name}
                      </button>
                      <div className="text-xs text-muted-foreground">{target.plugin_name ?? 'No plugin'}</div>
                    </td>
                    <td className="py-3 pr-4">
                      {target.covering_jobs.length ? (
                        <div className="space-y-1">
                          {target.covering_jobs.map((job) => (
                            <button
                              type="button"
                              key={job.job_id}
                              className="block text-left hover:text-[hsl(var(--accent))]"
                              onClick={() => navigate('/jobs', { state: { openJobId: job.job_id } })}
                            >
                              {job.name}
                            </button>
                          ))}
                        </div>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="py-3 pr-4">
                      {target.latest_attempt ? (
                        <>
                          <div className="capitalize">{target.latest_attempt.status}</div>
                          <div className="text-xs text-muted-foreground">
                            {formatLocalDateTime(target.latest_attempt.started_at)}
                          </div>
                        </>
                      ) : (
                        <span className="text-muted-foreground">Never</span>
                      )}
                    </td>
                    <td className="py-3 pr-4">
                      {target.latest_success ? (
                        <>
                          <div>{formatAge(target.latest_success.age_seconds)}</div>
                          <div className="text-xs text-muted-foreground">
                            {formatLocalDateTime(target.latest_success.finished_at)}
                          </div>
                        </>
                      ) : (
                        <span className="text-muted-foreground">Never</span>
                      )}
                    </td>
                    <td className="py-3 pr-4 tabular-nums">
                      {target.next_run_at ? formatLocalDateTime(target.next_run_at) : '—'}
                    </td>
                    <td className="py-3 pr-4 text-center font-mono tabular-nums">
                      {target.consecutive_failures}
                    </td>
                    <td className="py-3">
                      {target.gap_reason ? (
                        <span className="inline-flex rounded-full bg-red-500/10 px-2 py-1 text-xs font-medium text-red-500">
                          {gapLabels[target.gap_reason]}
                        </span>
                      ) : (
                        <span className="inline-flex rounded-full bg-green-500/10 px-2 py-1 text-xs font-medium text-green-500">
                          No gap
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </AppCard>
      <div className="grid gap-6 lg:grid-cols-2">
        <AppCard title="Recent Runs" description={`Last ${RECENT_RUNS_LIMIT} runs`} onTitleClick={() => navigate('/runs')}>
          {topRecentRuns.length === 0 ? (
            <div className="text-sm text-muted-foreground">No recent runs.</div>
          ) : (
            <ul className="divide-y divide-border">
              {topRecentRuns.map((r) => (
                <li key={r.id} className="py-3 flex items-center justify-between text-sm cursor-pointer" onClick={() => navigate('/runs', { state: { openRunId: r.id } })}>
                  <div className="flex items-center gap-3 min-w-0">
                    <div className={
                      'h-2.5 w-2.5 rounded-full ' +
                      (r.status === 'success' ? 'bg-green-500' : r.status === 'failed' ? 'bg-red-500' : 'bg-yellow-500')
                    } />
                    <div className="flex-1 min-w-0">
                      <div className="truncate font-medium">{r.job?.name ?? `Job ${r.job_id}`}</div>
                      <div className="text-muted-foreground">{formatLocalDateTime(r.started_at)}</div>
                    </div>
                  </div>
                  <span className={
                    'ml-4 shrink-0 rounded-full px-2 py-0.5 text-xs ' +
                    (r.status === 'success' ? 'bg-green-500/10 text-green-500' : r.status === 'failed' ? 'bg-red-500/10 text-red-500' : 'bg-yellow-500/10 text-yellow-500')
                  }>
                    {r.status}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </AppCard>
        <AppCard title="Upcoming Jobs" description={`Next ${UPCOMING_JOBS_LIMIT} scheduled jobs`} onTitleClick={() => navigate('/jobs')}>
          {upcoming && upcoming.length > 0 ? (
            <ul className="divide-y divide-border">
              {upcoming.map((u) => (
                <li key={u.job_id} className="py-3 flex items-center justify-between text-sm cursor-pointer" onClick={() => navigate(`/jobs`, { state: { openJobId: u.job_id } })}>
                  <div className="truncate font-medium">{u.name}</div>
                  <span
                    className="ml-4 shrink-0 rounded-full border border-[hsl(var(--accent)/.35)] bg-[hsl(var(--accent)/.12)] text-[hsl(var(--accent))] px-2 py-0.5 text-xs font-mono tabular-nums"
                  >
                    {formatLocalDateTime(u.next_run_at)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-sm text-muted-foreground">No jobs scheduled.</div>
          )}
        </AppCard>
      </div>
    </div>
  )
}
