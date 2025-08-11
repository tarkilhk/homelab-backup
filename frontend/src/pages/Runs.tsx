import { useEffect, useMemo, useState, Fragment } from 'react'
import { ChevronRight, ChevronDown, CheckCircle2, AlertTriangle, XCircle, X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { api, type RunWithJob, type TargetRun } from '../api/client'
import { formatLocalDateTime, formatLocalDateTimeShort } from '../lib/dates'
import { useLocation } from 'react-router-dom'
import AppCard from '../components/ui/AppCard'

// Use shared date utility that parses naive UTC and renders in local TZ
const formatShortDateTime = (dt?: string | null): string =>
  dt ? formatLocalDateTimeShort(dt) : '—'

// Human-readable byte size formatter (e.g., 12.3 MB)
const formatBytes = (value?: number | null): string => {
  if (value === null || value === undefined || isNaN(value)) return '—'
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

const formatBytesWithRaw = (value?: number | null): string => {
  if (value === null || value === undefined || isNaN(value)) return '—'
  return `${formatBytes(value)} (${value.toLocaleString()} bytes)`
}

// Map run/target-run status to a text color class for messages
function statusTextColorClass(status?: string): string {
  switch (status) {
    case 'success':
      return 'text-green-700'
    case 'failed':
      return 'text-red-700'
    case 'partial':
      return 'text-amber-600'
    case 'running':
      return 'text-gray-700'
    default:
      return ''
  }
}

export default function RunsPage() {
  const location = useLocation() as unknown as { state?: { openRunId?: number } }
  const openRunId = location?.state?.openRunId
  const [status, setStatus] = useState<string>('')
  const [startDate, setStartDate] = useState<string>('')
  const [endDate, setEndDate] = useState<string>('')
  const [tagFilterId, setTagFilterId] = useState<number | ''>('')
  const [detailsRun, setDetailsRun] = useState<RunWithJob | null>(null)

  const runsQueryKey = useMemo(
    () => ['runs', { status, startDate, endDate, tagFilterId }],
    [status, startDate, endDate, tagFilterId]
  )

  const { data, isLoading, error } = useQuery({
    queryKey: runsQueryKey,
    queryFn: () =>
      api.listRuns({
        status: status || undefined,
        start_date: startDate ? (startDate.includes('T') ? startDate : `${startDate}T00:00`) : undefined,
        end_date: endDate ? (endDate.includes('T') ? endDate : `${endDate}T23:59`) : undefined,
        tag_id: typeof tagFilterId === 'number' ? tagFilterId : undefined,
      }),
  })

  const { data: targets } = useQuery({ queryKey: ['targets'], queryFn: api.listTargets })
  const { data: tags } = useQuery({ queryKey: ['tags'], queryFn: api.listTags })
  const targetIdToName = useMemo(() => {
    const map = new Map<number, string>()
    for (const t of targets ?? []) map.set(t.id, t.name)
    return map
  }, [targets])

  const tagIdToName = useMemo(() => {
    const map = new Map<number, string>()
    for (const t of (tags ?? [])) map.set(t.id, t.display_name)
    return map
  }, [tags])

  const runs = useMemo(() => {
    const items = [...(data ?? [])]
    items.sort((a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime())
    return items.slice(0, 20)
  }, [data])
  // If navigated here with an openRunId (from dashboard), auto-open details
  useEffect(() => {
    if (!openRunId) return
    const found = (data ?? []).find((r) => r.id === openRunId) || runs.find((r) => r.id === openRunId)
    if (found) setDetailsRun(found)
  }, [openRunId, data, runs])

  // We now use simple date-only inputs. Normalization to start/end of day
  // happens right before sending the API request (see queryFn below).

  const [expandedRunIds, setExpandedRunIds] = useState<Set<number>>(new Set())

  const toggleExpanded = (runId: number) => {
    setExpandedRunIds((prev) => {
      const next = new Set(prev)
      if (next.has(runId)) next.delete(runId)
      else next.add(runId)
      return next
    })
  }

  function isClickFromInteractive(target: any): boolean {
    let el: HTMLElement | null = target as HTMLElement
    while (el) {
      const tag = el.tagName?.toLowerCase()
      if (tag && ['button', 'a', 'input', 'select', 'textarea', 'label', 'svg', 'path'].includes(tag)) return true
      if (el.getAttribute && el.getAttribute('role') === 'button') return true
      el = el.parentElement
    }
    return false
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">Past Runs</h1>
      </div>

      <AppCard
        title=""
        description=""
        className="overflow-x-auto"
        headerRight={(
          <div className="flex flex-wrap items-end gap-3">
            <label className="grid gap-1">
              <span className="text-sm font-medium">Status</span>
              <select
                id="runs-status-filter"
                className="border rounded px-3 py-2 bg-background"
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                aria-label="Status"
              >
                <option value="">All</option>
                <option value="running">running</option>
                <option value="success">success</option>
                <option value="failed">failed</option>
              </select>
            </label>
            <label className="grid gap-1">
              <span className="text-sm font-medium">From</span>
              <input
                id="runs-start-date"
                type="date"
                className="border rounded px-3 py-2 bg-background"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                aria-label="From"
              />
            </label>
            <label className="grid gap-1">
              <span className="text-sm font-medium">To</span>
              <input
                id="runs-end-date"
                type="date"
                className="border rounded px-3 py-2 bg-background"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                aria-label="To"
              />
            </label>
            <label className="grid gap-1">
              <span className="text-sm font-medium">Tag</span>
              <select
                id="runs-tag-filter"
                className="border rounded px-3 py-2 bg-background min-w-[12rem]"
                value={tagFilterId}
                onChange={(e) => setTagFilterId(e.target.value ? Number(e.target.value) : '')}
                aria-label="Tag"
              >
                <option value="">All</option>
                {(tags ?? ([] as Array<{ id: number; display_name: string }>)).map((t) => (
                  <option key={t.id} value={t.id}>{t.display_name}</option>
                ))}
              </select>
            </label>
            <button
              className="text-sm font-medium underline underline-offset-2 text-[hsl(var(--accent))] rounded px-2 py-1 hover:bg-[hsl(var(--accent)/.12)] cursor-pointer"
              title="Clear filters"
              onClick={() => { setStatus(''); setStartDate(''); setEndDate(''); setTagFilterId('') }}
            >
              Clear filters
            </button>
          </div>
        )}
      >
        <table className="min-w-full text-sm">
          <colgroup>
            <col style={{ width: '2.5rem' }} />
            <col style={{ width: '28%' }} />
            <col style={{ width: '12%' }} />
            <col style={{ width: '10%' }} />
            <col style={{ width: '18%' }} />
            <col style={{ width: '18%' }} />
            <col style={{ width: '12%' }} />
          </colgroup>
          <thead className="bg-muted/50 text-left">
               <tr>
              <th className="px-4 py-2 w-10" aria-label="Expand" />
              <th className="px-4 py-2">Job</th>
              <th className="px-4 py-2">Tag</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2">Started</th>
              <th className="px-4 py-2">Finished</th>
              <th className="px-4 py-2 text-right">Details</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr><td className="px-4 py-3" colSpan={7}>Loading...</td></tr>
            ) : error ? (
              <tr><td className="px-4 py-3 text-red-600" colSpan={7}>{String(error)}</td></tr>
            ) : runs.length === 0 ? (
              <tr><td className="px-4 py-3" colSpan={7}>No runs yet.</td></tr>
            ) : (
              runs.map((r: RunWithJob) => (
                <Fragment key={r.id}>
                  <tr
                    className="border-t hover:bg-muted/30 cursor-pointer"
                    onClick={(e) => {
                      if (isClickFromInteractive(e.target)) return
                      toggleExpanded(r.id)
                    }}
                  >
                    <td className="px-2 py-2">
                       <button
                        aria-label={expandedRunIds.has(r.id) ? 'Collapse' : 'Expand'}
                         className="inline-flex h-7 w-7 items-center justify-center rounded hover:bg-muted text-[hsl(var(--accent))] cursor-pointer"
                        onClick={(e) => { e.stopPropagation(); toggleExpanded(r.id) }}
                      >
                        {expandedRunIds.has(r.id) ? (
                          <ChevronDown className="h-5 w-5" />
                        ) : (
                          <ChevronRight className="h-5 w-5" />
                        )}
                      </button>
                    </td>
                    <td className="px-4 py-2">{r.job?.name ?? `Job #${r.job_id}`}</td>
                    <td className="px-4 py-2">{tagIdToName.get(r.job?.tag_id as number) ?? '—'}</td>
                    <td className="px-4 py-2">
                      {r.status === 'success' && <CheckCircle2 className="h-5 w-5 text-green-600" aria-label="success" />}
                      {r.status === 'failed' && <XCircle className="h-5 w-5 text-red-600" aria-label="failed" />}
                      {r.status === 'partial' && <AlertTriangle className="h-5 w-5 text-amber-500" aria-label="partial" />}
                      {r.status === 'running' && <AlertTriangle className="h-5 w-5 text-gray-500" aria-label="running" />}
                    </td>
                    <td className="px-4 py-2">{formatShortDateTime(r.started_at)}</td>
                    <td className="px-4 py-2">{formatShortDateTime(r.finished_at)}</td>
                    <td className="px-4 py-2 text-right">
                      {r.status === 'failed' || r.message || r.logs_text ? (
                        <button
                          className="text-sm font-medium underline text-[hsl(var(--accent))] cursor-pointer"
                          onClick={(e) => { e.stopPropagation(); setDetailsRun(r) }}
                        >
                          View
                        </button>
                      ) : '—'}
                    </td>
                  </tr>
                  {expandedRunIds.has(r.id) && (
                    <ExpandedTargetRunRows runId={r.id} targetIdToName={targetIdToName} />
                  )}
                </Fragment>
              ))
            )}
          </tbody>
        </table>
      </AppCard>

      {detailsRun && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center" onClick={() => setDetailsRun(null)}>
          <div className="bg-background border rounded-md shadow-xl max-w-2xl w-full mx-4" onClick={(e) => e.stopPropagation()}>
            <div className="p-3 border-b flex items-center">
              <div className="font-semibold">Run #{detailsRun.id} — {detailsRun.job?.name ?? `Job #${detailsRun.job_id}`}</div>
              <button aria-label="Close" className="ml-auto text-sm cursor-pointer" onClick={() => setDetailsRun(null)}>
                <X className="h-5 w-5 text-red-500" />
              </button>
            </div>
            <div className="p-4 space-y-3">
              <div className="grid gap-3 md:grid-cols-2">
                <div>
                  <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Started</div>
                  <div className="text-sm">{formatLocalDateTime(detailsRun.started_at)}</div>
                </div>
                <div>
                  <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Finished</div>
                  <div className="text-sm">{detailsRun.finished_at ? formatLocalDateTime(detailsRun.finished_at) : '—'}</div>
                </div>
              </div>
              
              <div>
                <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Status</div>
                <div className="flex items-center gap-2">
                  {detailsRun.status === 'success' && <CheckCircle2 className="h-5 w-5 text-green-600" aria-label="success" />}
                  {detailsRun.status === 'failed' && <XCircle className="h-5 w-5 text-red-600" aria-label="failed" />}
                  {detailsRun.status === 'partial' && <AlertTriangle className="h-5 w-5 text-amber-500" aria-label="partial" />}
                  {detailsRun.status !== 'success' && detailsRun.status !== 'failed' && detailsRun.status !== 'partial' && (
                    <AlertTriangle className="h-5 w-5 text-gray-500" aria-label={detailsRun.status} />
                  )}
                  <span className="text-sm text-[hsl(var(--foreground))]">{detailsRun.status}</span>
                </div>
              </div>
              {detailsRun.message && (
                <div>
                  <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Message</div>
                  <div className={`whitespace-pre-wrap ${statusTextColorClass(detailsRun.status)} text-sm`}>{detailsRun.message}</div>
                </div>
              )}
              {detailsRun.logs_text && (
                <div>
                  <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Logs</div>
                  <pre className="bg-muted/40 p-3 rounded max-h-80 overflow-auto whitespace-pre-wrap text-sm">{detailsRun.logs_text}</pre>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function ExpandedTargetRunRows({ runId, targetIdToName }: { runId: number; targetIdToName: Map<number, string> }) {
  const { data, isLoading, error } = useQuery({ queryKey: ['run', runId], queryFn: () => api.getRun(runId) })
  const items = data?.target_runs ?? []
  const [detailsTr, setDetailsTr] = useState<TargetRun | null>(null)
  const [copiedToast, setCopiedToast] = useState<string | null>(null)

  async function copyTextToClipboard(text: string): Promise<boolean> {
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(text)
        return true
      }
    } catch {
      // fall through to legacy path
    }
    try {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.setAttribute('readonly', '')
      ta.style.position = 'fixed'
      ta.style.opacity = '0'
      ta.style.left = '-9999px'
      document.body.appendChild(ta)
      ta.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(ta)
      return ok
    } catch {
      return false
    }
  }

  if (isLoading) {
    return (
      <tr className="bg-muted/30 border-t"><td className="px-2 py-2" /><td className="px-4 py-2" colSpan={6}><div className="text-xs text-gray-600">Loading target runs…</div></td></tr>
    )
  }
  if (error) {
    return (
      <tr className="bg-muted/30 border-t"><td className="px-2 py-2" /><td className="px-4 py-2" colSpan={6}><div className="text-xs text-red-600">{String(error)}</div></td></tr>
    )
  }
  if (items.length === 0) {
    return (
      <tr className="bg-muted/30 border-t"><td className="px-2 py-2" /><td className="px-4 py-2" colSpan={6}><div className="text-xs">No target runs.</div></td></tr>
    )
  }

  return (
    <>
      {/* spacer row separating parent row from child rows */}
      <tr className="bg-muted/40 border-t">
        <td className="px-2 py-1" />
        <td className="px-4 py-1" colSpan={6} />
      </tr>
      {items.map((tr: TargetRun, idx: number) => (
        <tr key={tr.id} className="bg-muted/30 border-t align-middle text-xs">
          <td className="px-2 py-1" />
          <td className="px-0 py-1 text-center">
            <span className="inline-flex items-center justify-center rounded-md bg-[hsl(var(--accent))] text-white text-xs font-medium px-2 py-0.5 shadow-sm min-w-[2rem]">
              {idx + 1}/{items.length}
            </span>
          </td>
          <td className="px-4 py-1">{targetIdToName.get(tr.target_id) ?? tr.target_id}</td>
          <td className="px-4 py-1">
            {tr.status === 'success' && <CheckCircle2 className="h-4 w-4 text-green-600" aria-label="success" />}
            {tr.status === 'failed' && <XCircle className="h-4 w-4 text-red-600" aria-label="failed" />}
            {tr.status === 'partial' && <AlertTriangle className="h-4 w-4 text-amber-500" aria-label="partial" />}
            {tr.status !== 'success' && tr.status !== 'failed' && tr.status !== 'partial' && (
              <AlertTriangle className="h-4 w-4 text-gray-500" aria-label={tr.status} />
            )}
          </td>
          <td className="px-4 py-1">{formatShortDateTime(tr.started_at)}</td>
          <td className="px-4 py-1">{formatShortDateTime(tr.finished_at)}</td>
          <td className="px-4 py-1 text-right">
            {(tr.status === 'failed' || !!tr.message || !!tr.logs_text) ? (
              <button
                className="text-[11px] underline text-[hsl(var(--accent))]"
                onClick={() => setDetailsTr(tr)}
              >
                View
              </button>
            ) : '—'}
          </td>
        </tr>
      ))}

      {detailsTr && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center" onClick={() => setDetailsTr(null)}>
          <div className="bg-background border rounded-md shadow-xl max-w-2xl w-full mx-4" onClick={(e) => e.stopPropagation()}>
            <div className="p-3 border-b flex items-center">
              <div className="font-semibold">TargetRun #{detailsTr.id} — {targetIdToName.get(detailsTr.target_id) ?? detailsTr.target_id}</div>
              <button aria-label="Close" className="ml-auto text-sm cursor-pointer" onClick={() => setDetailsTr(null)}>
                <X className="h-5 w-5 text-red-500" />
              </button>
            </div>
            <div className="p-4 grid gap-3 md:grid-cols-2">
              <div>
                <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Started</div>
                <div className="text-sm">{formatLocalDateTime(detailsTr.started_at)}</div>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Finished</div>
                <div className="text-sm">{detailsTr.finished_at ? formatLocalDateTime(detailsTr.finished_at) : '—'}</div>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide text-[hsl(var(--muted-foreground))]">Status</div>
                <div className="flex items-center gap-2">
                  {detailsTr.status === 'success' && <CheckCircle2 className="h-5 w-5 text-green-600" aria-label="success" />}
                  {detailsTr.status === 'failed' && <XCircle className="h-5 w-5 text-red-600" aria-label="failed" />}
                  {detailsTr.status === 'partial' && <AlertTriangle className="h-5 w-5 text-amber-500" aria-label="partial" />}
                  {detailsTr.status !== 'success' && detailsTr.status !== 'failed' && detailsTr.status !== 'partial' && (
                    <AlertTriangle className="h-5 w-5 text-gray-500" aria-label={detailsTr.status} />
                  )} 
                  <span className="text-sm text-[hsl(var(--foreground))]">{detailsTr.status}</span>
                </div>
              </div>
              <div className="md:col-span-2">
                <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Artifact</div>
                <div className="text-sm break-all">{detailsTr.artifact_path ?? '—'}</div>
              </div>
              <div>
                <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Size</div>
                <div className="text-sm">{formatBytesWithRaw(detailsTr.artifact_bytes as number)}</div>
              </div>
              <div className="md:col-span-2">
                <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">SHA256</div>
                <div className="text-sm break-all">
                  {detailsTr.sha256 ? (
                    <button
                      type="button"
                      className="font-mono break-all cursor-pointer underline decoration-dotted"
                      title="Click to copy SHA256"
                      onClick={async () => {
                        const ok = await copyTextToClipboard(detailsTr.sha256 as string)
                        setCopiedToast(ok ? 'sha256 copied to clipboard' : 'failed to copy sha256')
                        window.setTimeout(() => setCopiedToast(null), 1400)
                      }}
                    >
                      {detailsTr.sha256}
                    </button>
                  ) : '—'}
                </div>
              </div>
              
              {detailsTr.message && (
                <div className="md:col-span-2">
                  <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Message</div>
                  <div className={`text-sm whitespace-pre-wrap ${statusTextColorClass(detailsTr.status)}`}>{detailsTr.message}</div>
                </div>
              )}
              {detailsTr.logs_text && (
                <div className="md:col-span-2">
                  <div className="text-xs uppercase tracking-wide italic text-[hsl(var(--muted-foreground))]">Logs</div>
                  <pre className="bg-muted/40 p-3 rounded max-h-80 overflow-auto whitespace-pre-wrap text-sm">{detailsTr.logs_text}</pre>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
      {copiedToast && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 bg-black/80 text-white text-xs px-3 py-2 rounded shadow">
          {copiedToast}
        </div>
      )}
    </>
  )
}

