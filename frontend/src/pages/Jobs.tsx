import { useParams, useNavigate, Link } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type JobCreate, type Job, type Target } from '../api/client'
import { Button } from '../components/ui/button'
import { Trash2, Pencil } from 'lucide-react'

export default function JobsPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const targetId = useMemo(() => (id !== undefined ? Number(id) : null), [id])

  const { data: target } = useQuery({
    queryKey: ['target', targetId],
    queryFn: () => api.getTarget(targetId as number),
    enabled: Number.isFinite(targetId as number),
  })

  // When no targetId in route, allow picking a target from existing ones
  const { data: targets } = useQuery({
    queryKey: ['targets'],
    queryFn: api.listTargets,
    enabled: !Number.isFinite(targetId as number),
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

  // Update defaults when a specific target page is used
  useEffect(() => {
    if (Number.isFinite(targetId as number) && target) {
      setForm((prev) => ({
        ...prev,
        name: prev.name || `${target.name} Backup`,
        schedule_cron: prev.schedule_cron || '0 2 * * *',
      }))
    }
  }, [target?.name, targetId, target])

  const createMut = useMutation({
    mutationFn: (payload: JobCreate) => api.createJob(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      navigate('/targets')
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
          {!Number.isFinite(targetId as number) && (
            <label className="grid gap-1">
              <span className="text-sm">Target</span>
              <select
                className="border rounded px-3 py-2 bg-background"
                value={selectedTargetId}
                onChange={(e) => setSelectedTargetId(e.target.value === '' ? '' : Number(e.target.value))}
                required
              >
                <option value="" disabled>Select a target…</option>
                {(targets ?? []).map((t: Target) => (
                  <option key={t.id} value={t.id}>{t.name}</option>
                ))}
              </select>
            </label>
          )}
          <label className="grid gap-1">
            <span className="text-sm">Job Name</span>
            <input
              className="border rounded px-3 py-2"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />
          </label>
          <label className="grid gap-1">
            <span className="text-sm">Cron</span>
            <input
              className="border rounded px-3 py-2 font-mono"
              placeholder="0 2 * * *"
              value={form.schedule_cron}
              onChange={(e) => setForm({ ...form, schedule_cron: e.target.value })}
              required
            />
            <span className="text-xs text-gray-500">Use standard 5-field crontab, e.g., 0 2 * * * for 2:00 AM daily.</span>
          </label>
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
        <div className="p-4 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left">
                <th className="px-4 py-2 w-[30%]">Name</th>
                {!Number.isFinite(targetId as number) && (<th className="px-4 py-2 w-[20%]">Target</th>)}
                <th className="px-4 py-2 w-[20%]">Cron</th>
                <th className="px-4 py-2 w-[10%]">Enabled</th>
                <th className="px-4 py-2">Created</th>
                <th className="px-4 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {((jobs ?? []) as Job[])
                .filter((j) => (Number.isFinite(targetId as number) ? j.target_id === (targetId as number) : true))
                .map((j) => (
                  <tr key={j.id} className="border-t align-top">
                    <td className="px-4 py-2">{j.name}</td>
                    {!Number.isFinite(targetId as number) && (
                      <td className="px-4 py-2">{(targets ?? []).find((t) => t.id === j.target_id)?.name ?? target?.name ?? '—'}</td>
                    )}
                    <td className="px-4 py-2 font-mono">{j.schedule_cron}</td>
                    <td className="px-4 py-2">{j.enabled}</td>
                    <td className="px-4 py-2">{new Date(j.created_at).toLocaleString()}</td>
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


