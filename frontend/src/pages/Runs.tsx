import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type RunWithJob, type Target } from '../api/client'
import { formatLocalDateTime } from '../lib/dates'

export default function RunsPage() {
  const [status, setStatus] = useState<string>('')
  const [startDate, setStartDate] = useState<string>('')
  const [endDate, setEndDate] = useState<string>('')
  const [targetId, setTargetId] = useState<number | ''>('')
  const [detailsRun, setDetailsRun] = useState<RunWithJob | null>(null)

  const runsQueryKey = useMemo(
    () => ['runs', { status, startDate, endDate, targetId }],
    [status, startDate, endDate, targetId]
  )

  const { data, isLoading, error } = useQuery({
    queryKey: runsQueryKey,
    queryFn: () =>
      api.listRuns({
        status: status || undefined,
        start_date: startDate ? (startDate.includes('T') ? startDate : `${startDate}T00:00`) : undefined,
        end_date: endDate ? (endDate.includes('T') ? endDate : `${endDate}T23:59`) : undefined,
        target_id: typeof targetId === 'number' ? targetId : undefined,
      }),
  })

  const { data: targets } = useQuery({ queryKey: ['targets'], queryFn: api.listTargets })
  const targetIdToName = useMemo(() => {
    const map = new Map<number, string>()
    for (const t of targets ?? []) map.set(t.id, t.name)
    return map
  }, [targets])

  const runs = useMemo(() => {
    const items = [...(data ?? [])]
    items.sort((a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime())
    return items.slice(0, 20)
  }, [data])


  // We now use simple date-only inputs. Normalization to start/end of day
  // happens right before sending the API request (see queryFn below).

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">Runs</h1>
        <p className="text-sm text-gray-600">Last 20 runs.</p>
      </div>

      <section className="rounded-md border overflow-x-auto">
        <div className="p-3 bg-muted/30 border-b flex flex-wrap gap-3 items-end">
          <div>
            <label className="block text-xs text-gray-600" htmlFor="runs-status-filter">Status</label>
            <select
              id="runs-status-filter"
              className="border rounded px-2 py-1 bg-background"
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              <option value="">All</option>
              <option value="running">running</option>
              <option value="success">success</option>
              <option value="failed">failed</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-600" htmlFor="runs-start-date">Start date</label>
            <input
              id="runs-start-date"
              type="date"
              className="border rounded px-2 py-1 bg-background"
              value={startDate}
              onChange={(e) => setStartDate(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs text-gray-600" htmlFor="runs-end-date">End date</label>
            <input
              id="runs-end-date"
              type="date"
              className="border rounded px-2 py-1 bg-background"
              value={endDate}
              onChange={(e) => setEndDate(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs text-gray-600" htmlFor="runs-target-filter">Target</label>
            <select
              id="runs-target-filter"
              className="border rounded px-2 py-1 bg-background min-w-[12rem]"
              value={targetId}
              onChange={(e) => setTargetId(e.target.value ? Number(e.target.value) : '')}
            >
              <option value="">All</option>
              {(targets ?? ([] as Target[])).map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          </div>
          <button
            className="ml-auto text-xs underline text-[hsl(var(--accent))]"
            onClick={() => { setStatus(''); setStartDate(''); setEndDate(''); setTargetId('') }}
          >
            Clear filters
          </button>
        </div>
        <table className="min-w-full text-sm">
          <thead className="bg-muted/50 text-left">
            <tr>
              <th className="px-4 py-2">ID</th>
              <th className="px-4 py-2">Job</th>
              <th className="px-4 py-2">Target</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2">Started</th>
              <th className="px-4 py-2">Finished</th>
              <th className="px-4 py-2">Artifact</th>
              <th className="px-4 py-2">Details</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr><td className="px-4 py-3" colSpan={8}>Loading...</td></tr>
            ) : error ? (
              <tr><td className="px-4 py-3 text-red-600" colSpan={8}>{String(error)}</td></tr>
            ) : runs.length === 0 ? (
              <tr><td className="px-4 py-3" colSpan={8}>No runs yet.</td></tr>
            ) : (
              runs.map((r: RunWithJob) => (
                <tr key={r.id} className="border-t">
                  <td className="px-4 py-2">{r.id}</td>
                  <td className="px-4 py-2">{r.job?.name ?? `Job #${r.job_id}`}</td>
                  <td className="px-4 py-2">{r.job?.target_id ? (targetIdToName.get(r.job.target_id) ?? r.job.target_id) : '—'}</td>
                  <td className="px-4 py-2">
                    <span className={
                      r.status === 'success' ? 'text-green-600' : r.status === 'failed' ? 'text-red-600' : 'text-gray-700'
                    }>
                      {r.status}
                    </span>
                  </td>
                  <td className="px-4 py-2">{formatLocalDateTime(r.started_at)}</td>
                  <td className="px-4 py-2">{r.finished_at ? formatLocalDateTime(r.finished_at) : '—'}</td>
                  <td className="px-4 py-2">
                    {r.artifact_path ? (
                      <span title={r.artifact_path}>Artifact</span>
                    ) : '—'}
                  </td>
                  <td className="px-4 py-2">
                    {r.status === 'failed' || r.message || r.logs_text ? (
                      <button
                        className="text-xs underline text-[hsl(var(--accent))] cursor-pointer"
                        onClick={() => setDetailsRun(r)}
                      >
                        View
                      </button>
                    ) : '—'}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>

      {detailsRun && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center" onClick={() => setDetailsRun(null)}>
          <div className="bg-background border rounded-md shadow-xl max-w-2xl w-full mx-4" onClick={(e) => e.stopPropagation()}>
            <div className="p-3 border-b flex items-center">
              <div className="font-semibold">Run #{detailsRun.id} — {detailsRun.job?.name ?? `Job #${detailsRun.job_id}`}</div>
              <button className="ml-auto text-sm cursor-pointer" onClick={() => setDetailsRun(null)}>Close</button>
            </div>
            <div className="p-4 space-y-3">
              <div>
                <div className="text-xs text-gray-600">Status</div>
                <div>{detailsRun.status}</div>
              </div>
              {detailsRun.message && (
                <div>
                  <div className="text-xs text-gray-600">Message</div>
                  <div className="whitespace-pre-wrap text-red-700">{detailsRun.message}</div>
                </div>
              )}
              {detailsRun.logs_text && (
                <div>
                  <div className="text-xs text-gray-600">Logs</div>
                  <pre className="bg-muted/40 p-3 rounded max-h-80 overflow-auto whitespace-pre-wrap">{detailsRun.logs_text}</pre>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}


