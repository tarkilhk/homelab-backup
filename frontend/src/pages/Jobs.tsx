import { useParams, useLocation } from 'react-router-dom'
import { useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient, useQueries } from '@tanstack/react-query'
import { api, type JobCreate, type Job, type Tag, type TagTargetAttachment, type RetentionRule, type RetentionPolicy } from '../api/client'
import { formatLocalDateTime } from '../lib/dates'
import { Button } from '../components/ui/button'
import { useConfirm } from '../components/ConfirmProvider'
import {
  Trash2,
  Pencil,
  Play,
  Check,
  X,
  Plus,
  Target as TargetIcon,
  Sparkles,
  Loader2,
} from 'lucide-react'
import AppCard from '../components/ui/AppCard'
import IconButton from '../components/IconButton'

export default function JobsPage() {
  const confirm = useConfirm()
  const location = useLocation() as unknown as { state?: { openJobId?: number } }
  const preselectJobId = location?.state?.openJobId
  const { id } = useParams()
  const qc = useQueryClient()
  const targetId = useMemo(() => (id !== undefined ? Number(id) : null), [id])

  const { data: target } = useQuery({
    queryKey: ['target', targetId],
    queryFn: () => api.getTarget(targetId as number),
    enabled: Number.isFinite(targetId as number),
  })

  // Tags for selection and display
  const { data: tags } = useQuery({ queryKey: ['tags'], queryFn: api.listTags })
  const tagIdToName = useMemo(() => {
    const map = new Map<number, string>()
    for (const t of (tags as Tag[] | undefined) ?? []) map.set(t.id, t.display_name)
    return map
  }, [tags])

  // Jobs listing for table below
  const { data: jobs } = useQuery({
    queryKey: ['jobs'],
    queryFn: api.listJobs,
  })

  // Build a list of unique tag IDs from jobs to lazily fetch target counts
  const jobTagIds: number[] = useMemo(() => {
    const ids = new Set<number>()
    for (const j of ((jobs ?? []) as Job[])) ids.add(j.tag_id)
    return Array.from(ids)
  }, [jobs])

  // Fetch target lists per tag for all tags present in jobs (used for counts + hover tooltips)
  const tagTargetsQueries = useQueries({
    queries: jobTagIds.map((tid) => ({
      queryKey: ['tag', tid, 'targets'],
      queryFn: () => api.listTargetsForTag(tid),
      staleTime: 30_000,
    })),
  })

  const { tagIdToTargetCount, tagIdToTargetNames } = useMemo(() => {
    const countMap = new Map<number, number>()
    const namesMap = new Map<number, string[]>()
    jobTagIds.forEach((tid, idx) => {
      const list = tagTargetsQueries[idx]?.data as TagTargetAttachment[] | undefined
      if (Array.isArray(list)) {
        countMap.set(tid, list.length)
        namesMap.set(tid, list.map((x) => x.target.name))
      }
    })
    return { tagIdToTargetCount: countMap, tagIdToTargetNames: namesMap }
  }, [jobTagIds, tagTargetsQueries])

  // Selected tag when using global jobs page
  const [selectedTagId, setSelectedTagId] = useState<number | ''>('')

  const [form, setForm] = useState<{
    name: string
    schedule_cron: string
    enabled: string
  }>({
    name: '',
    schedule_cron: '',
    enabled: 'true',
  })

  // Retention override state
  const [retentionOverride, setRetentionOverride] = useState<'global' | 'custom'>('global')
  const [retentionDaily, setRetentionDaily] = useState<number>(7)
  const [retentionWeekly, setRetentionWeekly] = useState<number>(4)
  const [retentionMonthly, setRetentionMonthly] = useState<number>(6)

  // Build retention policy JSON for per-job override
  const buildRetentionPolicyJson = (): string | null => {
    if (retentionOverride === 'global') return null
    const rules: RetentionRule[] = []
    if (retentionDaily > 0) rules.push({ unit: 'day', window: retentionDaily, keep: 1 })
    if (retentionWeekly > 0) rules.push({ unit: 'week', window: retentionWeekly, keep: 1 })
    if (retentionMonthly > 0) rules.push({ unit: 'month', window: retentionMonthly, keep: 1 })
    if (rules.length === 0) return null
    return JSON.stringify({ rules } as RetentionPolicy)
  }

  // Parse retention policy from job
  const loadRetentionFromJob = (job: Job) => {
    if (job.retention_policy_json) {
      try {
        const policy: RetentionPolicy = JSON.parse(job.retention_policy_json)
        if (policy.rules && policy.rules.length > 0) {
          setRetentionOverride('custom')
          for (const rule of policy.rules) {
            if (rule.unit === 'day') setRetentionDaily(rule.window)
            else if (rule.unit === 'week') setRetentionWeekly(rule.window)
            else if (rule.unit === 'month') setRetentionMonthly(rule.window)
          }
          return
        }
      } catch { /* ignore */ }
    }
    setRetentionOverride('global')
    setRetentionDaily(7)
    setRetentionWeekly(4)
    setRetentionMonthly(6)
  }

  // Reset retention state
  const resetRetentionState = () => {
    setRetentionOverride('global')
    setRetentionDaily(7)
    setRetentionWeekly(4)
    setRetentionMonthly(6)
  }

  // Track whether the user has opted-in to dynamic name suggestions via the
  // sparkles button. When false, we never modify a non-empty name.
  const [nameSuggested, setNameSuggested] = useState<boolean>(false)

  // Edit/Delete state (edit happens via the top form)
  const [editingId, setEditingId] = useState<number | null>(null)
  // Controls visibility of the create/edit card; hidden by default
  const [showEditor, setShowEditor] = useState<boolean>(false)

  // Transient per-job status after clicking Run Now: pending/success/error
  const [runStatusByJobId, setRunStatusByJobId] = useState<
    Partial<Record<number, 'success' | 'error'>>
  >({})
  const [runPendingByJobId, setRunPendingByJobId] = useState<
    Partial<Record<number, boolean>>
  >({})

  // Filters for jobs table
  const [filters, setFilters] = useState<{
    status: '' | 'true' | 'false'
    tagId: number | ''
  }>({ status: '', tagId: '' })
  

  // No date or name filters currently

  const filteredJobs: Job[] = useMemo(() => {
    return ((jobs ?? []) as Job[])
      .filter((j) => (filters.status ? String(j.enabled) === filters.status : true))
      .filter((j) => (filters.tagId ? j.tag_id === filters.tagId : true))
  }, [jobs, filters])

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

  // Known name prefixes derived from cron cadence
  const knownPrefixes = ['Daily', 'Weekly', 'Monthly'] as const

  // Remove any leading known prefix(es) from a name. Handles repeated
  // application like "Daily Daily foo" and exact matches like "Daily".
  function stripKnownPrefix(name: string): string {
    let s = name ?? ''
    // Remove repeated prefixes like "Daily Daily foo"
    // Always match the word and the trailing space when present
    // to avoid leaving stray spaces behind
    // Also handle exact match (e.g., s === 'Daily') at the end
    for (;;) {
      const match = knownPrefixes.find((p) => s.startsWith(`${p} `))
      if (!match) break
      s = s.slice(match.length + 1)
    }
    if (knownPrefixes.some((p) => s === p)) return ''
    return s
  }

  // Build a suggested name using current cron, target or selected tag
  function buildSuggestedName(prevName?: string): string | null {
    const prefix = inferPrefixFromCron(form.schedule_cron)
    const strippedPrev = stripKnownPrefix((prevName ?? '').trim())
    const tName = target?.name
    const selectedTagName = (() => {
      if (selectedTagId === '' || Number.isFinite(targetId as number)) return null
      const name = tagIdToName.get(selectedTagId as number)
      return name ?? null
    })()
    const suffix = Number.isFinite(targetId as number) && tName
      ? `${tName} Backup`
      : selectedTagName
        ? `${selectedTagName} Backup`
        : (strippedPrev || 'Backup')
    if (!suffix && !prefix) return null
    return [prefix, suffix].filter(Boolean).join(' ')
  }

  // Update defaults when a specific target page is used
  useLayoutEffect(() => {
    if (Number.isFinite(targetId as number) && target) {
      setForm((prev) => ({
        ...prev,
        name: prev.name || `${target.name} Backup`,
        schedule_cron: prev.schedule_cron || '0 2 * * *',
      }))
      // Reset suggestion state when auto-seeding from target
      setNameSuggested(false)
    }
  }, [target?.name, targetId, target])

  // Auto-suggest name when cron or tag changes, but only if the name is empty
  // or the user explicitly opted-in by clicking the sparkles button.
  useEffect(() => {
    setForm((prev) => {
      const currentName = (prev.name ?? '').trim()
      const allowUpdate = currentName === '' || nameSuggested
      if (!allowUpdate) return prev
      const suggested = buildSuggestedName(prev.name)
      if (!suggested || suggested === prev.name) return prev
      return { ...prev, name: suggested }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.schedule_cron, selectedTagId])

  // (Removed per new behavior; handled by combined effect above.)

  const createMut = useMutation({
    mutationFn: (payload: JobCreate) => api.createJob(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      // Reset the form after creation
      if (Number.isFinite(targetId as number) && target) {
        setForm({ name: `${target.name} Backup`, schedule_cron: '0 2 * * *', enabled: 'true' })
      } else {
        setForm({ name: '', schedule_cron: '', enabled: 'true' })
        setSelectedTagId('')
      }
      resetRetentionState()
      setShowEditor(false)
    },
  })

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: number; body: { name: string; schedule_cron: string; enabled: boolean; tag_id?: number; retention_policy_json?: string | null } }) =>
      api.updateJob(id, body),
    onSuccess: () => {
      setEditingId(null)
      qc.invalidateQueries({ queryKey: ['jobs'] })
      resetRetentionState()
      setShowEditor(false)
    },
  })

  const deleteMut = useMutation({
    mutationFn: (id: number) => api.deleteJob(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
    },
  })

  // If navigated with openJobId, preload the edit form with that job
  useEffect(() => {
    if (!preselectJobId || !Array.isArray(jobs)) return
    const j = (jobs as Job[]).find((it) => it.id === preselectJobId)
    if (j) {
      setEditingId(j.id)
      setForm({ name: j.name, schedule_cron: j.schedule_cron, enabled: String(j.enabled) as 'true' | 'false' })
      loadRetentionFromJob(j)
      if (!Number.isFinite(targetId as number)) {
        setSelectedTagId(j.tag_id)
      }
      setShowEditor(true)
    }
  }, [preselectJobId, jobs, targetId])

  // Trigger a manual run for a job
  const runNowMut = useMutation({
    mutationFn: (id: number) => api.runJobNow(id),
    onSuccess: () => {
      // Refresh runs if anyone is observing them
      qc.invalidateQueries({ queryKey: ['runs'] })
    },
  })

  // If on a target-specific page, find its AUTO tag to use as default tag selection
  const { data: autoTagId } = useQuery({
    queryKey: ['target', targetId, 'auto-tag'],
    queryFn: async () => {
      const list = await api.listTargetTags(targetId as number)
      const auto = list.find((tt) => tt.origin === 'AUTO')
      return auto?.tag.id ?? null
    },
    enabled: Number.isFinite(targetId as number),
  })

  // For the form panel, compute the current tag whose targets will be affected
  const currentFormTagId = useMemo(() => {
    return Number.isFinite(targetId as number) ? (autoTagId ?? null) : (selectedTagId === '' ? null : (selectedTagId as number))
  }, [autoTagId, selectedTagId, targetId])

  // Fetch dynamic target count for the currently selected tag in the form
  const { data: currentFormTagTargets } = useQuery({
    queryKey: ['tag', currentFormTagId, 'targets'],
    queryFn: async () => await api.listTargetsForTag(currentFormTagId as number),
    enabled: Number.isFinite(currentFormTagId as unknown as number),
    staleTime: 15_000,
  })
  const currentFormTagTargetCount = (currentFormTagTargets ?? []).length
  const currentFormTagTargetNames = (currentFormTagTargets ?? []).map((t) => t.target.name)

  useEffect(() => {
    // When viewing jobs scoped to a target, always reflect that target's AUTO tag
    if (Number.isFinite(targetId as number) && autoTagId) {
      setSelectedTagId(autoTagId as number)
      setFilters((f) => ({ ...f, tagId: autoTagId as number }))
    }
  }, [autoTagId, targetId])

  // When landing on a target-scoped Jobs page from Targets, open the editor panel by default
  useEffect(() => {
    if (Number.isFinite(targetId as number) && autoTagId && !showEditor && editingId === null) {
      setShowEditor(true)
    }
  }, [targetId, autoTagId, showEditor, editingId])

  // Cancel editing helper (used by Cancel button and Escape key)
  function cancelEditing(): void {
    setEditingId(null)
    if (Number.isFinite(targetId as number) && target) {
      setForm({ name: `${target.name} Backup`, schedule_cron: '0 2 * * *', enabled: 'true' })
    } else {
      setForm({ name: '', schedule_cron: '', enabled: 'true' })
      setSelectedTagId('')
    }
    setNameSuggested(false)
    resetRetentionState()
    setShowEditor(false)
  }

  // Global Escape handler to cancel edit when editor is open
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape' && (showEditor || editingId !== null)) {
        e.preventDefault()
        cancelEditing()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [showEditor, editingId, target, targetId])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Jobs</h1>
          <p className="text-sm text-muted-foreground">
            {Number.isFinite(targetId as number)
              ? 'Create a job for this target.'
              : 'Pick a target and create a job.'}
          </p>
        </div>
        <IconButton
          variant="accent"
          aria-label="Add Job"
          onClick={() => {
            setEditingId(null)
            setForm({ name: '', schedule_cron: '', enabled: 'true' })
            setSelectedTagId('')
            setNameSuggested(false)
            resetRetentionState()
            setShowEditor(true)
          }}
        >
          <Plus className="h-4 w-4" aria-hidden="true" /> Add Job
        </IconButton>
      </div>

      {(showEditor || editingId !== null) && (
        <AppCard title={editingId ? 'Update Job' : (Number.isFinite(targetId as number) ? (target ? target.name : 'Target') : 'New Job')}>
        <form
          className="grid gap-4 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (editingId) {
              updateMut.mutate({
                id: editingId,
                body: {
                  name: form.name,
                  schedule_cron: form.schedule_cron,
                  enabled: form.enabled === 'true',
                  tag_id: Number.isFinite(targetId as number) ? (autoTagId ?? undefined) : (selectedTagId as number),
                  retention_policy_json: buildRetentionPolicyJson(),
                },
              })
            } else {
              const tagToUse = Number.isFinite(targetId as number)
                ? (autoTagId as number | null)
                : (selectedTagId as number)
              if (!tagToUse) return
              const payload: JobCreate = {
                tag_id: tagToUse,
                name: form.name,
                schedule_cron: form.schedule_cron,
                enabled: form.enabled === 'true',
                retention_policy_json: buildRetentionPolicyJson(),
              }
              createMut.mutate(payload)
            }
          }}
        >
          {(!Number.isFinite(targetId as number)) ? (
            // Global Jobs page: render Tag and Cron with aligned input row
            <div className="sm:col-span-2 grid gap-x-4">
              {/* Label row */}
              <div className="grid sm:grid-cols-2 gap-x-4">
                <label className="text-sm" htmlFor="job-tag-select">Tag</label>
                <label className="text-sm" htmlFor="cron-input">Cron</label>
              </div>
              {/* Input row (aligned) */}
              <div className="grid sm:grid-cols-2 gap-x-4">
                <div>
                  <select
                    id="job-tag-select"
                    className="border rounded px-3 py-2 bg-background w-full"
                    value={selectedTagId}
                    onChange={(e) => setSelectedTagId(e.target.value === '' ? '' : Number(e.target.value))}
                    aria-label="Tag"
                    required
                  >
                    <option value="" disabled>Select a tag…</option>
                    {(tags ?? []).map((t: Tag) => (
                      <option key={t.id} value={t.id}>{t.display_name}</option>
                    ))}
                  </select>
                  {currentFormTagId && (
                    <div
                      className="mt-2 inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs text-muted-foreground"
                      title={(currentFormTagTargetNames.length ? currentFormTagTargetNames.join('\n') : '')}
                    >
                      <TargetIcon className="h-3.5 w-3.5" />
                      <span>{currentFormTagTargetCount || '—'}</span>
                      <span className="opacity-75">targets</span>
                    </div>
                  )}
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
            // Target-specific page: show fixed Tag (AUTO) and Cron fields aligned like the global layout
            <div className="sm:col-span-2 grid gap-x-4">
              {/* Label row */}
              <div className="grid sm:grid-cols-2 gap-x-4">
                <label className="text-sm" htmlFor="job-tag-select">Tag</label>
                <label className="text-sm" htmlFor="cron-input">Cron</label>
              </div>
              {/* Input row */}
              <div className="grid sm:grid-cols-2 gap-x-4">
                <div>
                  <select
                    id="job-tag-select"
                    className="border rounded px-3 py-2 bg-muted/50 w-full"
                    value={currentFormTagId ?? ''}
                    onChange={() => { /* Tag is fixed to AUTO for the target */ }}
                    aria-label="Tag"
                    disabled
                  >
                    <option value="">Select a tag…</option>
                    {(tags ?? []).map((t: Tag) => (
                      <option key={t.id} value={t.id}>{t.display_name}</option>
                    ))}
                  </select>
                  {currentFormTagId && (
                    <div
                      className="mt-2 inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs text-muted-foreground"
                      title={(currentFormTagTargetNames.length ? currentFormTagTargetNames.join('\n') : '')}
                    >
                      <TargetIcon className="h-3.5 w-3.5" />
                      <span>{currentFormTagTargetCount || '—'}</span>
                      <span className="opacity-75">targets</span>
                    </div>
                  )}
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
          )}
          <div className="grid gap-1 sm:col-start-1 sm:row-start-2">
            <label className="text-sm" htmlFor="name-input">Job Name</label>
            <div className="flex items-center gap-2">
              <button
                type="button"
                aria-label="Suggest name"
                title="Suggest name"
                className="p-2 rounded hover:bg-muted text-[hsl(var(--accent))]"
                onClick={() => {
                  const suggested = buildSuggestedName(form.name)
                  if (suggested) {
                    setForm((prev) => ({ ...prev, name: suggested }))
                    setNameSuggested(true)
                  }
                }}
              >
                <Sparkles className="h-4 w-4" />
              </button>
              <input
                id="name-input"
                className="border rounded px-3 py-2 flex-1"
                value={form.name}
                onChange={(e) => {
                  setForm({ ...form, name: e.target.value })
                }}
                required
              />
            </div>
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

          {/* Retention Override Section */}
          <div className="sm:col-span-2 space-y-3 border-t pt-4 mt-2">
            <div className="text-sm font-medium">Retention Policy</div>
            <div className="flex gap-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="retention-mode"
                  checked={retentionOverride === 'global'}
                  onChange={() => setRetentionOverride('global')}
                />
                <span className="text-sm">Use global settings</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  name="retention-mode"
                  checked={retentionOverride === 'custom'}
                  onChange={() => setRetentionOverride('custom')}
                />
                <span className="text-sm">Override for this job</span>
              </label>
            </div>

            {retentionOverride === 'custom' && (
              <div className="grid gap-3 sm:grid-cols-3 bg-muted/30 rounded-lg p-3">
                <div className="space-y-1">
                  <label className="text-xs font-medium">Daily</label>
                  <div className="flex items-center gap-1 text-xs">
                    <span>Keep 1/day for</span>
                    <input
                      type="number"
                      min={0}
                      max={365}
                      className="w-12 border rounded px-1 py-0.5 text-xs"
                      value={retentionDaily}
                      onChange={(e) => setRetentionDaily(Math.max(0, parseInt(e.target.value) || 0))}
                    />
                    <span>days</span>
                  </div>
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-medium">Weekly</label>
                  <div className="flex items-center gap-1 text-xs">
                    <span>Keep 1/week for</span>
                    <input
                      type="number"
                      min={0}
                      max={52}
                      className="w-12 border rounded px-1 py-0.5 text-xs"
                      value={retentionWeekly}
                      onChange={(e) => setRetentionWeekly(Math.max(0, parseInt(e.target.value) || 0))}
                    />
                    <span>weeks</span>
                  </div>
                </div>
                <div className="space-y-1">
                  <label className="text-xs font-medium">Monthly</label>
                  <div className="flex items-center gap-1 text-xs">
                    <span>Keep 1/month for</span>
                    <input
                      type="number"
                      min={0}
                      max={120}
                      className="w-12 border rounded px-1 py-0.5 text-xs"
                      value={retentionMonthly}
                      onChange={(e) => setRetentionMonthly(Math.max(0, parseInt(e.target.value) || 0))}
                    />
                    <span>months</span>
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="sm:col-span-2 flex items-center gap-2">
            <Button type="submit" disabled={createMut.isPending || updateMut.isPending}>
              {editingId ? (updateMut.isPending ? 'Saving…' : 'Save') : (createMut.isPending ? 'Creating…' : 'Create Job')}
            </Button>
            <Button
              type="button"
              variant="cancel"
              onClick={cancelEditing}
            >
              Cancel
            </Button>
            {(createMut.error || updateMut.error) && (
              <span className="text-sm text-red-600">{String(createMut.error || updateMut.error)}</span>
            )}
          </div>
        </form>
        </AppCard>
      )}

      {/* Jobs table */}
      <AppCard title="Existing Jobs" className="overflow-x-auto">
        <div className="space-y-3">
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
              <label className="text-sm" htmlFor="jobs-filter-tag">Filter Tag</label>
              <select
                id="jobs-filter-tag"
                className="border rounded px-3 py-2 bg-background"
                value={filters.tagId === '' ? '' : String(filters.tagId)}
                onChange={(e) => setFilters((f) => ({ ...f, tagId: e.target.value === '' ? '' : Number(e.target.value) }))}
              >
                <option value="">All tags</option>
                {((tags ?? []) as Tag[]).map((t) => (
                  <option key={t.id} value={t.id}>{t.display_name}</option>
                ))}
              </select>
            </div>
            <div className="md:col-span-3">
              <button
                type="button"
                className="text-sm underline"
                onClick={() => setFilters({ status: '', tagId: '' })}
              >
                Clear filters
              </button>
            </div>
          </div>

           <table className="w-full text-sm">
            <thead>
              <tr className="text-left">
                <th className="px-4 py-2 w-[28%]">Name</th>
                <th className="px-4 py-2 w-[20%]">Tag</th>
                <th className="px-4 py-2 w-[12%]">Targets</th>
                <th className="px-4 py-2 w-[18%]">Cron</th>
                <th className="px-4 py-2 w-[10%]">Enabled</th>
                <th className="px-4 py-2">Created</th>
                <th className="px-4 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filteredJobs
                .map((j) => (
                  <tr
                    key={j.id}
                    className="border-t align-top hover:bg-muted/30 cursor-pointer"
                    onDoubleClick={(e) => {
                      const target = e.target as HTMLElement
                      let el: HTMLElement | null = target
                      let interactive = false
                      while (el) {
                        const tag = el.tagName?.toLowerCase()
                        if (tag && ['button', 'a', 'input', 'select', 'textarea', 'label', 'svg', 'path'].includes(tag)) { interactive = true; break }
                        if (el.getAttribute && el.getAttribute('role') === 'button') { interactive = true; break }
                        el = el.parentElement
                      }
                      if (interactive) return
                      // Prevent text selection caused by double-click and briefly disable selection on the row
                      e.preventDefault()
                      const row = e.currentTarget as HTMLElement
                      row.classList.add('select-none')
                      try { window.getSelection()?.removeAllRanges?.() } catch {}
                      window.setTimeout(() => row.classList.remove('select-none'), 300)
                      setEditingId(j.id)
                      setForm({ name: j.name, schedule_cron: j.schedule_cron, enabled: String(j.enabled) as 'true' | 'false' })
                      loadRetentionFromJob(j)
                      if (!Number.isFinite(targetId as number)) {
                        setSelectedTagId(j.tag_id)
                      }
                      setShowEditor(true)
                    }}
                  >
                    <td className="px-4 py-2">{j.name}</td>
                    <td className="px-4 py-2">{tagIdToName.get(j.tag_id) ?? '—'}</td>
                     <td className="px-4 py-2">
                       <div
                         className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs text-muted-foreground"
                         title={(tagIdToTargetNames.get(j.tag_id)?.join('\n')) || ''}
                       >
                         <TargetIcon className="h-3.5 w-3.5" />
                         <span>{tagIdToTargetCount.get(j.tag_id) ?? '—'}</span>
                       </div>
                     </td>
                    <td className="px-4 py-2 font-mono">{j.schedule_cron}</td>
                    <td className="px-4 py-2">{j.enabled ? 'true' : 'false'}</td>
                    <td className="px-4 py-2">{formatLocalDateTime(j.created_at)}</td>
                    <td className="px-4 py-2 text-right">
                      <div className="flex justify-end gap-2">
                        <button
                          aria-label="Edit"
                          className="p-2 rounded hover:bg-muted cursor-pointer"
                          onClick={() => {
                            setEditingId(j.id)
                            setForm({ name: j.name, schedule_cron: j.schedule_cron, enabled: String(j.enabled) as 'true' | 'false' })
                            loadRetentionFromJob(j)
                            if (!Number.isFinite(targetId as number)) {
                              setSelectedTagId(j.tag_id)
                            }
                            setShowEditor(true)
                          }}
                        >
                          <Pencil className="h-4 w-4" />
                        </button>
                        <button
                          aria-label="Run now"
                          className="p-2 rounded hover:bg-muted cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
                          onClick={async () => {
                            try {
                              setRunPendingByJobId((prev) => ({ ...prev, [j.id]: true }))
                              await runNowMut.mutateAsync(j.id)
                              setRunStatusByJobId((prev) => ({ ...prev, [j.id]: 'success' }))
                            } catch (err) {
                              setRunStatusByJobId((prev) => ({ ...prev, [j.id]: 'error' }))
                            } finally {
                              setRunPendingByJobId((prev) => ({ ...prev, [j.id]: false }))
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
                          disabled={Boolean(runPendingByJobId[j.id])}
                          title="Run now"
                        >
                          <span className="relative inline-flex h-4 w-4">
                            {/* Pending */}
                            <Loader2
                              className={
                                `absolute inset-0 h-4 w-4 text-green-600 transition-all duration-200 ease-out animate-spin ` +
                                `${runPendingByJobId[j.id] ? 'opacity-100 scale-100' : 'opacity-0 scale-75'}`
                              }
                            />
                            {/* Play (idle) */}
                            <Play
                              className={
                                `absolute inset-0 h-4 w-4 text-green-600 transition-all duration-200 ease-out ` +
                                `${runStatusByJobId[j.id] || runPendingByJobId[j.id] ? 'opacity-0 scale-75' : 'opacity-100 scale-100'}`
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
                          className="p-2 rounded hover:bg-muted cursor-pointer"
                          onClick={async () => {
                            const ok = await confirm({
                              title: `dev.tarkilnetwork:8081`,
                              description: `Delete job "${j.name}"? This cannot be undone.`,
                              confirmText: 'OK',
                              cancelText: 'Cancel',
                              variant: 'danger',
                            })
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
      </AppCard>
    </div>
  )
}
