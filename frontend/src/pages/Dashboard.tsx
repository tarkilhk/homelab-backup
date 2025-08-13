import { Activity, CalendarClock, Rocket, ShieldCheck, Puzzle } from 'lucide-react'
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import AppCard from '../components/ui/AppCard'
import { useNavigate } from 'react-router-dom'
import StatCard from '../components/StatCard'
import { api } from '../api/client'
import { formatLocalDateTime } from '../lib/dates'

export default function DashboardPage() {
  const navigate = useNavigate()
  const { data: targets } = useQuery({ queryKey: ['targets'], queryFn: api.listTargets })
  const { data: jobs } = useQuery({ queryKey: ['jobs'], queryFn: api.listJobs })
  const { data: plugins } = useQuery({ queryKey: ['plugins'], queryFn: api.listPlugins })

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


