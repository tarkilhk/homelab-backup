import { useParams, Link } from 'react-router-dom'
import { useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type JobCreate, type Job, type Target } from '../api/client'
import { formatLocalDateTime } from '../lib/dates'
import { Button } from '../components/ui/button'
import { Trash2, Pencil, Play, Check, X } from 'lucide-react'

export default function JobsPage() {
  const { id } = useParams()
  const qc = useQueryClient()
  const targetId = useMemo(() => (id !== undefined ? Number(id) : null), [id])

  const { data: target } = useQuery({
    queryKey: ['target', targetId],
    queryFn: () => api.getTarget(targetId as number),
    enabled: Number.isFinite(targetId as number),
  })

  // Always fetch targets for mapping names and filters
  const { data: targets } = useQuery({
    queryKey: ['targets'],
    queryFn: api.listTargets,
  })

  // Jobs listing for table below
  const { data: jobs } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.listJobs,
  })

  // Selected target when using global jobs page
  const [selectedTargetId, setSelectedTargetId] = useState<number | ''>('')

  const [form, setForm] = useState<{
    name: string
    schedule_cron: string
    enabled: string
  }>({
    name: '',
    schedule_cron: '',
    enabled: 'true',
  })

  // Edit/Delete state (edit happens via the top form)
  const [editingId, setEditingId] = useState<number | null>(null)

  // Transient per-job status after clicking Run Now: success/error for 2s
  const [runStatusByJobId, setRunStatusByJobId] = useState<
    Partial<Record<number, 'success' | 'error'>>
  >({})

  // Filters for jobs table
  const [filters, setFilters] = useState<{
    status: '' | 'true' | 'false'
    targetId: number | ''
  }>({ status: '', targetId: '' })

  const targetNameById = useMemo(() => {
    const map = new Map<number, string>()
    for (const t of (targets as Target[] | undefined) ?? []) {
      map.set(t.id, t.name)
    }
    return map
  }, [targets])

  // No date or name filters currently

  const filteredJobs: Job[] = useMemo(() => {
    return ((jobs ?? []) as Job[])
      .filter((j) => (Number.isFinite(targetId as number) ? j.target_id === (targetId as number) : true))
      .filter((j) => (filters.status ? j.enabled === filters.status : true))
      .filter((j) => (filters.targetId ? j.target_id === filters.targetId : true))
  }, [jobs, targetId, filters])

  // Humanize a 5-field cron expression for common cases
  function pad2(n: number): string { return n.toString().padStart(2, '0') }
  function formatAmPm(hour24: number, minute: number): string {
    const am = hour24 < 12
    const hour12 = ((hour24 % 12) || 12)
    return `${hour12}:${pad2(minute)} ${am ? 'AM' : 'PM'}`
  }
  function ordinal(n: number): string {
    const s = ['th', 'st', 'nd', 'rd']
    const v = n % 100
    return `${n}${s[(v - 20) % 10] || s[v] || s[0]}`
  }
  function cronToHuman(cron: string): string | null {
    const parts = cron.trim().split(/\s+/)
    if (parts.length !== 5) return null
    const [minS, hourS, domS, monS, dowS] = parts
    const min = /^\d+$/.test(minS) ? Number(minS) : null
    const hour = /^\d+$/.test(hourS) ? Number(hourS) : null

    // every N minutes
    const everyNMin = minS.match(/^\*\/(\d{1,2})$/)
    if (everyNMin && hourS === '*' && domS === '*' && monS === '*' && dowS === '*') {
      return `Every ${Number(everyNMin[1])} minutes`
    }

    // hourly at minute
    if (min !== null && hourS === '*' && domS === '*' && monS === '*' && dowS === '*') {
      return `Every hour at :${pad2(min)}`
    }

    // daily
    if (min !== null && hour !== null && domS === '*' && monS === '*' && dowS === '*') {
      return `Every day at ${formatAmPm(hour, min)}`
    }

    // weekly - single day-of-week 0-6 or 7
    if (min !== null && hour !== null && domS === '*' && monS === '*' && /^(?:[0-7]|[0-7]-[0-7]|[0-7](?:,[0-7])*)$/.test(dowS)) {
      const dayMap = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
      let dayText = dowS
      if (/^\d+$/.test(dowS)) dayText = dayMap[Number(dowS)]
      else if (/^\d-\d$/.test(dowS)) {
        const [a, b] = dowS.split('-').map(Number)
        dayText = `${dayMap[a]}-${dayMap[b]}`
      } else if (/^(?:\d,)+\d$/.test(dowS)) {
        dayText = dowS.split(',').map((d) => dayMap[Number(d)]).join(', ')
      }
      return `Every ${dayText} at ${formatAmPm(hour, min)}`
    }

    // monthly on day-of-month
    if (min !== null && hour !== null && /^\d+$/.test(domS) && monS === '*' && dowS === '*') {
      return `Every month on the ${ordinal(Number(domS))} at ${formatAmPm(hour, min)}`
    }

    return null
  }

  const humanCron = useMemo(() => cronToHuman(form.schedule_cron), [form.schedule_cron])

  // Infer prefix from cron for label updates
  function inferPrefixFromCron(cron: string): string | null {
    const trimmed = cron.trim()
    if (!trimmed) return null
    // Very simple recognizers for common cases
    // daily: any pattern with day-of-month and month as * and day-of-week as *
    // weekly: day-of-week 0-6 specified while day-of-month is *
    // monthly: day-of-month is a specific number and month is *
    const parts = trimmed.split(/\s+/)
    if (parts.length !== 5) return null
    const [, , dom, mon, dow] = parts
    if (dom === '*' && mon === '*' && dow === '*') return 'Daily'
    if (dom === '*' && mon === '*' && /^(?:[0-6]|[0-6](?:,[0-6])*)$/.test(dow)) return 'Weekly'
    if (/^\d+$/.test(dom) && mon === '*' && dow === '*') return 'Monthly'
    return null
  }

  // Update defaults when a specific target page is used
  useLayoutEffect(() => {
    if (Number.isFinite(targetId as number) && target) {
      setForm((prev) => ({
        ...prev,
        name: prev.name || `${target.name} Backup`,
        schedule_cron: prev.schedule_cron || '0 2 * * *',
      }))
    }
  }, [target?.name, targetId, target])

  // When selecting a target from the global Jobs page, seed a sensible default name
  useEffect(() => {
    if (!Number.isFinite(targetId as number) && selectedTargetId && Array.isArray(targets)) {
      const selected = (targets as Target[]).find((t) => t.id === selectedTargetId)
      if (selected) {
        setForm((prev) => ({
          ...prev,
          name: prev.name || `${selected.name} Backup`,
        }))
      }
    }
  }, [selectedTargetId, targetId, targets])

  // If user picks the standard daily cron, prefix the job name with "Daily "
  useEffect(() => {
    if (!Number.isFinite(targetId as number) && selectedTargetId && Array.isArray(targets)) {
      const selected = (targets as Target[]).find((t) => t.id === selectedTargetId)
      if (selected && form.schedule_cron === '0 2 * * *') {
        const baseName = `${selected.name} Backup`
        if (form.name === baseName) {
          setForm((prev) => ({ ...prev, name: `Daily ${baseName}` }))
        }
      }
    }
  }, [form.schedule_cron, selectedTargetId, targetId, targets])

  // Auto-fill job name when selecting a target from global page
  useEffect(() => {
    if (!Number.isFinite(targetId as number) && selectedTargetId && targets) {
      const t = (targets as Target[]).find((x) => x.id === selectedTargetId)
      if (t) {
        setForm((prev) => {
          const suffix = `${t.name} Backup`
          // Keep any existing prefix if present
          const maybePrefix = inferPrefixFromCron(prev.schedule_cron)
          const nextName = maybePrefix ? `${maybePrefix} ${suffix}` : suffix
          return { ...prev, name: nextName }
        })
      }
    }
  }, [selectedTargetId, targets, targetId])

  // Update name prefix based on cron selections
  useEffect(() => {
    // Only auto-prefix on the global Jobs page after user interactions
    if (Number.isFinite(targetId as number)) return
    const prefix = inferPrefixFromCron(form.schedule_cron)
    if (!prefix) return
    setForm((prev) => {
      // If name already includes a common prefix, replace it; else prefix
      const suffixFromTarget = (() => {
        // If a target is in route, use that; else try selected target
        const tName = target?.name || (targets as Target[] | undefined)?.find((t) => t.id === selectedTargetId)?.name
        if (!tName) return prev.name
        const base = `${tName} Backup`
        // If prev.name already ends with base (with or without prefix), reuse base
        // Extract suffix by removing any known prefix
        const knownPrefixes = ['Daily', 'Weekly', 'Monthly']
        for (const p of knownPrefixes) {
          if (prev.name.startsWith(`${p} `)) {
            const rest = prev.name.slice(p.length + 1)
            return rest
          }
        }
        return base
      })()
      const newName = `${prefix} ${suffixFromTarget}`
      if (newName === prev.name) return prev
      return { ...prev, name: newName }
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.schedule_cron])

  const createMut = useMutation({
    mutationFn: (payload: JobCreate) => api.createJob(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      // Reset the form after creation
      if (Number.isFinite(targetId as number) && target) {
        setForm({ name: `${target.name} Backup`, schedule_cron: '0 2 * * *', enabled: 'true' })
      } else {
        setForm({ name: '', schedule_cron: '', enabled: 'true' })
        setSelectedTargetId('')
      }
    },
  })

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: number; body: { name: string; schedule_cron: string; enabled: string } }) =>
      api.updateJob(id, body),
    onSuccess: () => {
      setEditingId(null)
      qc.invalidateQueries({ queryKey: ['jobs'] })
    },
  })

  const deleteMut = useMutation({
    mutationFn: (id: number) => api.deleteJob(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
    },
  })

  // Trigger a manual run for a job
  const runNowMut = useMutation({
    mutationFn: (id: number) => api.runJobNow(id),
    onSuccess: () => {
      // Refresh runs if anyone is observing them
      qc.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Jobs</h1>
          <p className="text-sm text-gray-600">
            {Number.isFinite(targetId as number)
              ? 'Create a job for this target.'
              : 'Pick a target and create a job.'}
          </p>
        </div>
        <Link to="/targets" className="text-sm underline">Back to Targets</Link>
      </div>

      <section className="rounded-md border">
        <div className="p-4 border-b font-medium">
          {Number.isFinite(targetId as number)
            ? (target ? target.name : 'Target')
            : 'New Job'}
        </div>
        <form
          className="p-4 grid gap-4 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (editingId) {
              updateMut.mutate({
                id: editingId,
                body: { name: form.name, schedule_cron: form.schedule_cron, enabled: form.enabled },
              })
            } else {
              const idToUse = Number.isFinite(targetId as number)
                ? (target as Target)?.id
                : (selectedTargetId as number)
              if (!idToUse) return
              const payload: JobCreate = {
                target_id: idToUse,
                name: form.name,
                schedule_cron: form.schedule_cron,
                enabled: form.enabled,
              }
              createMut.mutate(payload)
            }
          }}
        >
          {(!Number.isFinite(targetId as number)) ? (
            // Global Jobs page: render Target and Cron with aligned input row
            <div className="sm:col-span-2 grid gap-x-4">
              {/* Label row */}
              <div className="grid sm:grid-cols-2 gap-x-4">
                <label className="text-sm" htmlFor="job-target-select">Target</label>
                <label className="text-sm" htmlFor="cron-input">Cron</label>
              </div>
              {/* Input row (aligned) */}
              <div className="grid sm:grid-cols-2 gap-x-4">
                <div>
                  <select
                    id="job-target-select"
                    className="border rounded px-3 py-2 bg-background w-full"
                    value={selectedTargetId}
                    onChange={(e) => setSelectedTargetId(e.target.value === '' ? '' : Number(e.target.value))}
                    aria-label="Target"
                    required
                  >
                    <option value="" disabled>Select a target…</option>
                    {(targets ?? []).map((t: Target) => (
                      <option key={t.id} value={t.id}>{t.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <div className="flex items-center gap-2">
                    <input
                      id="cron-input"
                      className="border rounded px-3 py-2 font-mono w-44"
                      placeholder="0 2 * * *"
                      value={form.schedule_cron}
                      onChange={(e) => setForm({ ...form, schedule_cron: e.target.value })}
                      required
                    />
                    <span className="text-sm md:text-base text-gray-500 whitespace-nowrap" aria-live="polite">
                      {humanCron ?? '—'}
                    </span>
                  </div>
                  <span className="block mt-1 text-xs text-gray-500">Use standard 5-field crontab, e.g., 0 2 * * * for 2:00 AM daily.</span>
                </div>
              </div>
            </div>
          ) : (
            // Target-specific page: only Cron field
            <div className="grid gap-1 sm:col-start-1 sm:row-start-1">
              <label className="text-sm" htmlFor="cron-input">Cron</label>
              <div className="flex items-center gap-2">
                <input
                  id="cron-input"
                  className="border rounded px-3 py-2 font-mono w-44"
                  placeholder="0 2 * * *"
                  value={form.schedule_cron}
                  onChange={(e) => setForm({ ...form, schedule_cron: e.target.value })}
                  required
                />
                <span className="text-sm md:text-base text-gray-500 whitespace-nowrap" aria-live="polite">
                  {humanCron ?? '—'}
                </span>
              </div>
              <span className="text-xs text-gray-500">Use standard 5-field crontab, e.g., 0 2 * * * for 2:00 AM daily.</span>
            </div>
          )}
          <div className="grid gap-1 sm:col-start-1 sm:row-start-2">
            <label className="text-sm" htmlFor="name-input">Job Name</label>
            <input
              id="name-input"
              className="border rounded px-3 py-2"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />
          </div>
          <label className="grid gap-1">
            <span className="text-sm">Enabled</span>
            <select
              className="border rounded px-3 py-2 bg-background"
              value={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.value })}
            >
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
          </label>
          <div className="sm:col-span-2 flex items-center gap-2">
            <Button type="submit" disabled={createMut.isPending || updateMut.isPending}>
              {editingId ? (updateMut.isPending ? 'Saving…' : 'Save') : (createMut.isPending ? 'Creating…' : 'Create Job')}
            </Button>
            {editingId && (
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setEditingId(null)
                  if (Number.isFinite(targetId as number) && target) {
                    setForm({ name: `${target.name} Backup`, schedule_cron: '0 2 * * *', enabled: 'true' })
                  } else {
                    setForm({ name: '', schedule_cron: '', enabled: 'true' })
                    setSelectedTargetId('')
                  }
                }}
              >
                Cancel
              </Button>
            )}
            {(createMut.error || updateMut.error) && (
              <span className="text-sm text-red-600">{String(createMut.error || updateMut.error)}</span>
            )}
          </div>
        </form>
      </section>

      {/* Jobs table */}
      <section className="rounded-md border">
        <div className="p-4 border-b font-medium">Existing Jobs</div>
        <div className="p-4 space-y-3 overflow-x-auto">
          {/* Filters */}
          <div className="grid gap-3 md:grid-cols-3">
            <div className="grid gap-1">
              <label className="text-sm" htmlFor="jobs-filter-status">Status</label>
              <select
                id="jobs-filter-status"
                className="border rounded px-3 py-2 bg-background"
                value={filters.status}
                onChange={(e) => setFilters((f) => ({ ...f, status: e.target.value as 'true' | 'false' | '' }))}
              >
                <option value="">All</option>
                <option value="true">enabled</option>
                <option value="false">disabled</option>
              </select>
            </div>
            <div className="grid gap-1">
              <label className="text-sm" htmlFor="jobs-filter-target">Filter Target</label>
              <select
                id="jobs-filter-target"
                className="border rounded px-3 py-2 bg-background"
                value={filters.targetId === '' ? '' : String(filters.targetId)}
                onChange={(e) => setFilters((f) => ({ ...f, targetId: e.target.value === '' ? '' : Number(e.target.value) }))}
              >
                <option value="">All targets</option>
                {((targets ?? []) as Target[]).map((t) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </select>
            </div>
            <div className="md:col-span-3">
              <button
                type="button"
                className="text-sm underline"
                onClick={() => setFilters({ status: '', targetId: '' })}
              >
                Clear filters
              </button>
            </div>
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="text-left">
                <th className="px-4 py-2 w-[30%]">Name</th>
                <th className="px-4 py-2 w-[20%]">Target</th>
                <th className="px-4 py-2 w-[20%]">Cron</th>
                <th className="px-4 py-2 w-[10%]">Enabled</th>
                <th className="px-4 py-2">Created</th>
                <th className="px-4 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredJobs
                .map((j) => (
                  <tr key={j.id} className="border-t align-top">
                    <td className="px-4 py-2">{j.name}</td>
                    <td className="px-4 py-2">{targetNameById.get(j.target_id) ?? target?.name ?? '—'}</td>
                    <td className="px-4 py-2 font-mono">{j.schedule_cron}</td>
                    <td className="px-4 py-2">{j.enabled}</td>
                    <td className="px-4 py-2">{formatLocalDateTime(j.created_at)}</td>
                    <td className="px-4 py-2 text-right">
                      <div className="flex justify-end gap-2">
                        <button
                          aria-label="Edit"
                          className="p-2 rounded hover:bg-muted"
                          onClick={() => {
                            setEditingId(j.id)
                            setForm({ name: j.name, schedule_cron: j.schedule_cron, enabled: j.enabled })
                            if (!Number.isFinite(targetId as number)) {
                              setSelectedTargetId(j.target_id)
                            }
                          }}
                        >
                          <Pencil className="h-4 w-4" />
                        </button>
                        <button
                          aria-label="Run now"
                          className="p-2 rounded hover:bg-muted"
                          onClick={async () => {
                            try {
                              await runNowMut.mutateAsync(j.id)
                              setRunStatusByJobId((prev) => ({ ...prev, [j.id]: 'success' }))
                            } catch (err) {
                              setRunStatusByJobId((prev) => ({ ...prev, [j.id]: 'error' }))
                            } finally {
                              // Revert icon after 1.3 seconds
                              setTimeout(() => {
                                setRunStatusByJobId((prev) => {
                                  const next = { ...prev }
                                  delete next[j.id]
                                  return next
                                })
                              }, 1300)
                            }
                          }}
                          title="Run now"
                        >
                          <span className="relative inline-flex h-4 w-4">
                            {/* Play (idle) */}
                            <Play
                              className={
                                `absolute inset-0 h-4 w-4 text-green-600 transition-all duration-200 ease-out ` +
                                `${runStatusByJobId[j.id] ? 'opacity-0 scale-75' : 'opacity-100 scale-100'}`
                              }
                            />
                            {/* Success */}
                            <Check
                              className={
                                `absolute inset-0 h-4 w-4 text-green-600 transition-all duration-200 ease-out ` +
                                `${runStatusByJobId[j.id] === 'success' ? 'opacity-100 scale-100' : 'opacity-0 scale-75'}`
                              }
                            />
                            {/* Error */}
                            <X
                              className={
                                `absolute inset-0 h-4 w-4 text-red-600 transition-all duration-200 ease-out ` +
                                `${runStatusByJobId[j.id] === 'error' ? 'opacity-100 scale-100' : 'opacity-0 scale-75'}`
                              }
                            />
                          </span>
                        </button>
                        <button
                          aria-label="Delete"
                          className="p-2 rounded hover:bg-muted"
                          onClick={() => {
                            const ok = window.confirm(`Delete job "${j.name}"? This cannot be undone.`)
                            if (ok) deleteMut.mutate(j.id)
                          }}
                        >
                          <Trash2 className="h-4 w-4 text-red-600" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}


