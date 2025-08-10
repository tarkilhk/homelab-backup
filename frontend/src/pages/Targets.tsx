import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type Target, type PluginInfo, type TargetTagWithOrigin } from '../api/client'
import { useEffect, useState } from 'react'
import { Button } from '../components/ui/button'
import { Trash2, Pencil, Calendar, Check, X, Plus, Tag as TagIcon } from 'lucide-react'
import { formatLocalDateTime } from '../lib/dates'
import AppCard from '../components/ui/AppCard'
import IconButton from '../components/IconButton'
import { Link } from 'react-router-dom'
import { useConfirm } from '../components/ConfirmProvider'
import { toast } from 'sonner'

export default function TargetsPage() {
  const confirm = useConfirm()
  const qc = useQueryClient()
  const { data: targets, isLoading, error } = useQuery({
    queryKey: ['targets'],
    queryFn: api.listTargets,
  })
  const { data: plugins } = useQuery({
    queryKey: ['plugins'],
    queryFn: api.listPlugins,
  })

  const [form, setForm] = useState({
    name: '',
    plugin_name: '',
    plugin_config_json: '{}',
  })

  // Controls visibility of the create/edit card. Defaults to hidden.
  const [showEditor, setShowEditor] = useState<boolean>(false)

  // Schema-driven config state
  const [schema, setSchema] = useState<Record<string, any> | null>(null)
  const [config, setConfig] = useState<Record<string, any>>({})

  // When plugin changes, fetch its schema if available
  useEffect(() => {
    let cancelled = false
    async function run() {
      if (!form.plugin_name) {
        setSchema(null)
        setConfig({})
        return
      }
      try {
        const s = await api.getPluginSchema(form.plugin_name)
        if (!cancelled) {
          setSchema(s)
          // Seed config when editing; otherwise start empty
          if (editingId) {
            try {
              const parsed = JSON.parse(form.plugin_config_json || '{}')
              setConfig(parsed && typeof parsed === 'object' ? parsed : {})
            } catch {
              setConfig({})
            }
          } else {
            setConfig({})
          }
        }
      } catch (err) {
        // If schema is not available (404), fall back to raw JSON; otherwise notify
        if (!cancelled) {
          setSchema(null)
          if (editingId) {
            try {
              const parsed = JSON.parse(form.plugin_config_json || '{}')
              setConfig(parsed && typeof parsed === 'object' ? parsed : {})
            } catch {
              setConfig({})
            }
          } else {
            setConfig({})
          }
          const status = (err as any)?.status
          if (status !== 404) {
            const message = (err as any)?.message || 'Failed to load plugin schema'
            toast.error(message)
          }
        }
      }
    }
    run()
    return () => {
      cancelled = true
    }
  }, [form.plugin_name])

  const createMut = useMutation({
    mutationFn: api.createTarget,
    onSuccess: () => {
      setForm({ name: '', plugin_name: '', plugin_config_json: '{}' })
      setSchema(null)
      setConfig({})
      qc.invalidateQueries({ queryKey: ['targets'] })
      setShowEditor(false)
    },
  })

  // Edit/Delete state (edit happens via the top form)
  const [editingId, setEditingId] = useState<number | null>(null)

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: number; body: any }) => api.updateTarget(id, body),
    onSuccess: () => {
      setEditingId(null)
      qc.invalidateQueries({ queryKey: ['targets'] })
      setShowEditor(false)
    },
  })

  const deleteMut = useMutation({
    mutationFn: (id: number) => api.deleteTarget(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['targets'] })
    },
  })

  // Lazy tag fetching per target for tooltip display
  const [tagsStateByTargetId, setTagsStateByTargetId] = useState<
    Record<number, { status: 'idle' | 'loading' | 'success' | 'error'; data?: TargetTagWithOrigin[] }>
  >({})

  async function ensureTargetTagsLoaded(targetId: number) {
    const current = tagsStateByTargetId[targetId]
    if (current && (current.status === 'loading' || current.status === 'success')) return
    setTagsStateByTargetId((prev) => ({ ...prev, [targetId]: { status: 'loading' } }))
    try {
      const data = await api.listTargetTags(targetId)
      setTagsStateByTargetId((prev) => ({ ...prev, [targetId]: { status: 'success', data } }))
    } catch (err) {
      setTagsStateByTargetId((prev) => ({ ...prev, [targetId]: { status: 'error' } }))
      const message = (err as any)?.message || 'Failed to load target tags'
      toast.error(message)
    }
  }

  // On-demand per-target schedule computation using dedicated endpoint
  const [schedulesByTargetId, setSchedulesByTargetId] = useState<
    Record<number, { status: 'idle' | 'loading' | 'success' | 'error'; names?: string[] }>
  >({})

  useEffect(() => {
    const tlist = targets ?? []
    if (!tlist.length) return
    let cancelled = false
    async function load() {
      // Mark all as loading first
      setSchedulesByTargetId((prev) => {
        const next = { ...prev }
        for (const t of tlist) next[t.id] = { status: 'loading' }
        return next
      })
      try {
        await Promise.all(
          tlist.map(async (t) => {
            try {
              const names = await api.listTargetSchedules(t.id)
              if (cancelled) return
              setSchedulesByTargetId((prev) => ({ ...prev, [t.id]: { status: 'success', names } }))
            } catch {
              if (cancelled) return
              setSchedulesByTargetId((prev) => ({ ...prev, [t.id]: { status: 'error' } }))
            }
          })
        )
      } catch {
        // ignore; individual target errors handled above
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [JSON.stringify((targets ?? []).map((t) => t.id))])

  // Test status and error message for connectivity test
  const [testError, setTestError] = useState<string>('')
  const [testState, setTestState] = useState<'idle' | 'success' | 'error'>('idle')
  const testPluginMut = useMutation({
    mutationFn: async () => {
      if (!form.plugin_name) throw new Error('Pick a plugin first')
      const cfg = schema ? (config ?? {}) : (() => {
        try { return JSON.parse(form.plugin_config_json || '{}') } catch { return {} }
      })()
      return await api.testPlugin(form.plugin_name, cfg)
    },
    onSuccess: (res: { ok: boolean; error?: string }) => {
      if (res.ok) {
        setTestError('')
        setTestState('success')
      } else {
        setTestError(res.error || 'Test failed')
        setTestState('error')
      }
      // Revert visual after a short delay
      setTimeout(() => setTestState('idle'), 1300)
    },
    onError: () => {
      setTestError('Test failed')
      setTestState('error')
      setTimeout(() => setTestState('idle'), 1300)
    },
  })

  // Cancel editing helper
  function cancelEditing(): void {
    setEditingId(null)
    setForm({ name: '', plugin_name: '', plugin_config_json: '{}' })
    setSchema(null)
    setConfig({})
    setShowEditor(false)
    setTestError('')
    setTestState('idle')
  }

  // Escape key to cancel edit
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape' && (showEditor || editingId !== null)) {
        e.preventDefault()
        cancelEditing()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [showEditor, editingId])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Targets</h1>
          <p className="text-sm text-muted-foreground">List and create backup targets.</p>
        </div>
        <IconButton
          variant="accent"
          aria-label="Add Target"
          onClick={() => {
            setEditingId(null)
            setForm({ name: '', plugin_name: '', plugin_config_json: '{}' })
            setSchema(null)
            setConfig({})
            setShowEditor(true)
          }}
        >
          <Plus className="h-4 w-4" aria-hidden="true" /> Add Target
        </IconButton>
      </div>

      {(showEditor || editingId !== null) && (
        <AppCard title={editingId ? 'Edit Target' : 'Create Target'} description="Configure a plugin and its options">
          <form
          className="grid gap-4 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault()
            const payload = {
              name: form.name,
              plugin_name: form.plugin_name,
                plugin_config_json: schema ? JSON.stringify(config) : form.plugin_config_json,
            }
              if (editingId) {
                updateMut.mutate({ id: editingId, body: payload })
              } else {
                createMut.mutate(payload as any)
              }
          }}
        >
          <label className="grid gap-1">
            <span className="text-sm">Name</span>
            <input
              className="border rounded px-3 py-2"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
            />
          </label>
          {/* Slug removed from UI; generated by backend from name */}
          <label className="grid gap-1">
            <span className="text-sm">Plugin</span>
            <select
              className="border rounded px-3 py-2 bg-background"
              value={form.plugin_name}
              onChange={(e) => setForm({ ...form, plugin_name: e.target.value })}
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
          {form.plugin_name && schema ? (
            <fieldset className="grid gap-2 sm:col-span-2">
              <legend className="text-sm">Plugin Config</legend>
              {/* Render basic inputs for flat object schemas */}
              {Object.entries((schema as any).properties ?? {}).map(([key, def]: [string, any]) => {
                const required = Array.isArray((schema as any).required) && (schema as any).required.includes(key)
                const type = (def && def.type) || 'string'
                const format = def && def.format
                const id = `plugin-field-${key}`
                const value = (config as any)[key] ?? ''
                const label = def && def.title ? def.title : key

                if (type === 'boolean') {
                  return (
                    <label key={key} className="flex items-center gap-2">
                      <input
                        id={id}
                        aria-label={label}
                        type="checkbox"
                        checked={Boolean(value)}
                        onChange={(e) => setConfig({ ...config, [key]: e.target.checked })}
                      />
                      <span className="text-sm">{label}{required ? ' *' : ''}</span>
                    </label>
                  )
                }

                const inputType = type === 'number' || type === 'integer' ? 'number' : format === 'uri' ? 'url' : 'text'
                return (
                  <label key={key} className="grid gap-1">
                    <span className="text-sm">{label}{required ? ' *' : ''}</span>
                    <input
                      id={id}
                      aria-label={label}
                      className="border rounded px-3 py-2"
                      type={inputType}
                      placeholder={def && def.default !== undefined ? String(def.default) : undefined}
                      value={value}
                      onChange={(e) =>
                        setConfig({
                          ...config,
                          [key]: inputType === 'number' ? (e.target.value === '' ? '' : Number(e.target.value)) : e.target.value,
                        })
                      }
                    />
                  </label>
                )
              })}
            </fieldset>
          ) : (
            <label className="grid gap-1 sm:col-span-2">
              <span className="text-sm">Plugin Config JSON</span>
              <textarea
                className="border rounded px-3 py-2 h-28 font-mono text-sm"
                value={form.plugin_config_json}
                onChange={(e) => setForm({ ...form, plugin_config_json: e.target.value })}
                placeholder="{}"
              />
            </label>
          )}
          <div className="sm:col-span-2 flex items-center gap-2">
            <Button type="submit" disabled={createMut.isPending || updateMut.isPending}>
              {editingId ? (updateMut.isPending ? 'Saving...' : 'Save') : (createMut.isPending ? 'Creating...' : 'Create')}
            </Button>
            {/* Test connectivity (before saving target) */}
            <Button
              type="button"
              variant="outline"
              disabled={testPluginMut.isPending || !form.plugin_name}
              onClick={async () => {
                try {
                  await testPluginMut.mutateAsync()
                } catch {
                  // error handled in onError
                }
              }}
            >
              {testPluginMut.isPending ? (
                'Testing…'
              ) : (
                <span className="relative inline-flex items-center justify-center w-12">
                  {/* Idle label */}
                  <span
                    className={
                      `absolute transition-all duration-200 ease-out ` +
                      `${testState === 'idle' ? 'opacity-100 scale-100' : 'opacity-0 scale-75'}`
                    }
                  >
                    Test
                  </span>
                  {/* Success icon */}
                  <Check
                    className={
                      `absolute h-4 w-4 text-green-600 transition-all duration-200 ease-out ` +
                      `${testState === 'success' ? 'opacity-100 scale-100' : 'opacity-0 scale-75'}`
                    }
                  />
                  {/* Error icon */}
                  <X
                    className={
                      `absolute h-4 w-4 text-red-600 transition-all duration-200 ease-out ` +
                      `${testState === 'error' ? 'opacity-100 scale-100' : 'opacity-0 scale-75'}`
                    }
                  />
                </span>
              )}
            </Button>
            {/* Cancel next to Test */}
            <Button
              type="button"
              variant="cancel"
              onClick={cancelEditing}
            >
              Cancel
            </Button>
            {/* Right-aligned message area */}
            <span className="ml-auto text-sm min-h-5 text-right">
              {testError ? <span className="text-red-600">{testError}</span> : null}
            </span>
            {(createMut.error || updateMut.error) && (
              <span className="ml-3 text-sm text-red-600">{String(createMut.error || updateMut.error)}</span>
            )}
          </div>
        </form>
        </AppCard>
      )}

      <AppCard title="Existing Targets" className="overflow-x-auto ring-0 hover:ring-0 focus-within:ring-0">
        {isLoading ? (
          <div className="p-4 text-sm text-gray-600">Loading...</div>
        ) : error ? (
          <div className="p-4 text-sm text-red-600">{String(error)}</div>
        ) : (
           <table className="min-w-full text-sm">
            <thead className="bg-muted/50 text-left">
              <tr>
                <th className="px-4 py-2">Name</th>
                <th className="px-4 py-2">Plugin</th>
                <th className="px-4 py-2">Has Schedule?</th>
                <th className="px-4 py-2">Created</th>
                <th className="px-4 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {(targets ?? []).map((t: Target) => (
                <tr
                  key={t.id}
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
                    setEditingId(t.id)
                    setForm({
                      name: t.name,
                      plugin_name: t.plugin_name ?? '',
                      plugin_config_json: t.plugin_config_json ?? '{}',
                    })
                    try {
                      const parsed = JSON.parse(t.plugin_config_json || '{}')
                      setConfig(parsed && typeof parsed === 'object' ? parsed : {})
                    } catch {
                      setConfig({})
                    }
                    setShowEditor(true)
                  }}
                >
                  <td className="px-4 py-2 w-[20%]">{t.name}</td>
                  <td className="px-4 py-2 w-[20%]">{t.plugin_name ?? '—'}</td>
                  <td className="px-4 py-2">
                    {(() => {
                      const st = schedulesByTargetId[t.id]
                      if (!st || st.status === 'idle' || st.status === 'loading') return '—'
                      const names = st.names ?? []
                      return names.length ? (
                      <div className="relative group inline-flex items-center">
                        <Check className="h-4 w-4 text-green-600" aria-label="Has schedule" />
                        {/* Tooltip with schedule names */}
                        <div className="pointer-events-none absolute left-5 top-0 -translate-y-2 opacity-0 group-hover:opacity-100 group-hover:-translate-y-3 transition-all duration-150 ease-out z-20">
                          <div className="max-w-xs rounded-md border bg-popover text-popover-foreground shadow-md px-2.5 py-1.5 text-xs leading-relaxed whitespace-pre">
                            {names.join('\n')}
                          </div>
                        </div>
                      </div>
                      ) : (
                        <X className="h-4 w-4 text-red-600" aria-label="No schedule" />
                      )
                    })()}
                  </td>
                  <td className="px-4 py-2">{formatLocalDateTime(t.created_at)}</td>
                  <td className="px-4 py-2 text-right">
                    <div className="flex justify-end gap-2">
                      {/* Tag hover icon with modern tooltip */}
                      <div
                        className="relative group inline-flex items-center"
                        onMouseEnter={() => ensureTargetTagsLoaded(t.id)}
                      >
                        <span className="p-2 rounded text-muted-foreground cursor-default">
                          <TagIcon className="h-4 w-4" aria-hidden="true" />
                        </span>
                        {/* Tooltip */}
                        <div className="pointer-events-none absolute right-8 top-0 -translate-y-2 opacity-0 group-hover:opacity-100 group-hover:-translate-y-3 transition-all duration-150 ease-out z-20">
                          <div className="max-w-xs rounded-md border bg-popover text-popover-foreground shadow-md px-2.5 py-1.5 text-xs leading-relaxed whitespace-pre-wrap">
                            {(() => {
                              const st = tagsStateByTargetId[t.id]
                              if (!st || st.status === 'idle' || st.status === 'loading') return 'Loading tags…'
                              if (st.status === 'error') return 'Failed to load tags'
                              const names = (st.data ?? []).map((tt) => tt.tag.display_name)
                              return names.length ? names.join('  ') : 'No tags'
                            })()}
                          </div>
                        </div>
                      </div>
                      <button
                        aria-label="Edit"
                        className="p-2 rounded hover:bg-muted"
                        onClick={() => {
                          setEditingId(t.id)
                          setForm({
                            name: t.name,
                            plugin_name: t.plugin_name ?? '',
                            plugin_config_json: t.plugin_config_json ?? '{}',
                          })
                          // Seed config immediately; effect will refine after schema loads
                          try {
                            const parsed = JSON.parse(t.plugin_config_json || '{}')
                            setConfig(parsed && typeof parsed === 'object' ? parsed : {})
                          } catch {
                            setConfig({})
                          }
                          setShowEditor(true)
                        }}
                      >
                        <Pencil className="h-4 w-4" />
                      </button>
                      <Link
                        to={`/targets/${t.id}/jobs`}
                        aria-label="Jobs"
                        className="p-2 rounded hover:bg-muted"
                      >
                        <Calendar className="h-4 w-4" />
                      </Link>
                      <button
                        aria-label="Delete"
                        className="p-2 rounded hover:bg-muted"
                        onClick={async () => {
                          const ok = await confirm({
                            title: `dev.tarkilnetwork:8081`,
                            description: `Delete target "${t.name}"? This cannot be undone.`,
                            confirmText: 'OK',
                            cancelText: 'Cancel',
                            variant: 'danger',
                          })
                          if (ok) deleteMut.mutate(t.id)
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
        )}
      </AppCard>
    </div>
  )
}


