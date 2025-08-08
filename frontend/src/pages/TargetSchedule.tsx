import { useParams, useNavigate, Link } from 'react-router-dom'
import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type JobCreate, type PluginInfo } from '../api/client'
import { Button } from '../components/ui/button'

export default function TargetSchedulePage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const targetId = useMemo(() => Number(id), [id])

  const { data: target } = useQuery({
    queryKey: ['target', targetId],
    queryFn: () => api.getTarget(targetId),
    enabled: Number.isFinite(targetId),
  })

  const { data: plugins } = useQuery({
    queryKey: ['plugins'],
    queryFn: api.listPlugins,
  })

  const defaultPlugin = target?.plugin_name || (plugins && plugins[0]?.key) || ''

  const [form, setForm] = useState<{
    name: string
    schedule_cron: string
    enabled: string
    plugin: string
    plugin_version: string
  }>({
    name: target ? `${target.name} Backup` : 'Backup',
    schedule_cron: '0 2 * * *',
    enabled: 'true',
    plugin: defaultPlugin,
    plugin_version: (plugins && plugins[0]?.version) || '1.0.0',
  })

  // Update defaults when target/plugins load
  useEffect(() => {
    setForm((prev) => ({
      ...prev,
      name: target ? `${target.name} Backup` : prev.name,
      plugin: target?.plugin_name || prev.plugin || defaultPlugin,
      plugin_version: (plugins && plugins.find((p) => p.key === (target?.plugin_name || prev.plugin))?.version) || prev.plugin_version,
    }))
  }, [target?.name, target?.plugin_name, defaultPlugin, plugins])

  const createMut = useMutation({
    mutationFn: (payload: JobCreate) => api.createJob(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['jobs'] })
      navigate('/targets')
    },
  })

  if (!Number.isFinite(targetId)) {
    return <div className="p-4 text-sm text-red-600">Invalid target id.</div>
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Schedule Backup</h1>
          <p className="text-sm text-gray-600">Create a scheduled job for this target.</p>
        </div>
        <Link to="/targets" className="text-sm underline">Back to Targets</Link>
      </div>

      <section className="rounded-md border">
        <div className="p-4 border-b font-medium">{target ? target.name : 'Target'} — New Schedule</div>
        <form
          className="p-4 grid gap-4 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault()
            if (!target) return
            const payload: JobCreate = {
              target_id: target.id,
              name: form.name,
              schedule_cron: form.schedule_cron,
              enabled: form.enabled,
              plugin: form.plugin,
              plugin_version: form.plugin_version,
            }
            createMut.mutate(payload)
          }}
        >
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
            <span className="text-sm">Plugin</span>
            <select
              className="border rounded px-3 py-2 bg-background"
              value={form.plugin}
              onChange={(e) => setForm({ ...form, plugin: e.target.value })}
              required
            >
              <option value="" disabled>Select a plugin…</option>
              {(plugins ?? []).map((p: PluginInfo) => (
                <option key={p.key} value={p.key}>
                  {p.name ?? p.key}
                </option>
              ))}
            </select>
          </label>
          <label className="grid gap-1">
            <span className="text-sm">Plugin Version</span>
            <input
              className="border rounded px-3 py-2"
              value={form.plugin_version}
              onChange={(e) => setForm({ ...form, plugin_version: e.target.value })}
              required
            />
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
            <Button type="submit" disabled={createMut.isPending}>{createMut.isPending ? 'Creating…' : 'Create Schedule'}</Button>
            {createMut.error && (
              <span className="text-sm text-red-600">{String(createMut.error)}</span>
            )}
          </div>
        </form>
      </section>
    </div>
  )
}


