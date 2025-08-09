import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type Target, type PluginInfo } from '../api/client'
import { useEffect, useState } from 'react'
import { Button } from '../components/ui/button'
import { Trash2, Pencil, Calendar, Check, X, Plus } from 'lucide-react'
import AppCard from '../components/ui/AppCard'
import IconButton from '../components/IconButton'
import { Link } from 'react-router-dom'

export default function TargetsPage() {
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
      } catch (_err) {
        // If schema is not available (404), fall back to raw JSON
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
    },
  })

  // Edit/Delete state (edit happens via the top form)
  const [editingId, setEditingId] = useState<number | null>(null)

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: number; body: any }) => api.updateTarget(id, body),
    onSuccess: () => {
      setEditingId(null)
      qc.invalidateQueries({ queryKey: ['targets'] })
    },
  })

  const deleteMut = useMutation({
    mutationFn: (id: number) => api.deleteTarget(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['targets'] })
    },
  })

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

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Targets</h1>
          <p className="text-sm text-muted-foreground">List and create backup targets.</p>
        </div>
        <IconButton variant="accent" aria-label="Add Target">
          <Plus className="h-4 w-4" aria-hidden="true" /> Add Target
        </IconButton>
      </div>

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
            {/* Right-aligned message area */}
            <span className="ml-auto text-sm min-h-5 text-right">
              {testError ? <span className="text-red-600">{testError}</span> : null}
            </span>
            {editingId && (
              <Button
                type="button"
                variant="outline"
                onClick={() => {
                  setEditingId(null)
                  setForm({ name: '', plugin_name: '', plugin_config_json: '{}' })
                  setSchema(null)
                  setConfig({})
                }}
              >
                Cancel
              </Button>
            )}
            {(createMut.error || updateMut.error) && (
              <span className="ml-3 text-sm text-red-600">{String(createMut.error || updateMut.error)}</span>
            )}
          </div>
        </form>
      </AppCard>

      <AppCard title="Existing Targets" className="overflow-x-auto">
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
                <tr key={t.id} className="border-t align-top">
                  <td className="px-4 py-2 w-[20%]">{t.name}</td>
                  <td className="px-4 py-2 w-[20%]">{t.plugin_name ?? '—'}</td>
                  <td className="px-4 py-2">—</td>
                  <td className="px-4 py-2">{new Date(t.created_at).toLocaleString()}</td>
                  <td className="px-4 py-2 text-right">
                    <div className="flex justify-end gap-2">
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
                        onClick={() => {
                          const ok = window.confirm(`Delete target \"${t.name}\"? This cannot be undone.`)
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


