import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type Target } from '../api/client'
import { useState } from 'react'
import { Button } from '../components/ui/button'

export default function TargetsPage() {
  const qc = useQueryClient()
  const { data: targets, isLoading, error } = useQuery({
    queryKey: ['targets'],
    queryFn: api.listTargets,
  })

  const [form, setForm] = useState({ name: '', slug: '', type: '', config_json: '{}' })

  const createMut = useMutation({
    mutationFn: api.createTarget,
    onSuccess: () => {
      setForm({ name: '', slug: '', type: '', config_json: '{}' })
      qc.invalidateQueries({ queryKey: ['targets'] })
    },
  })

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Targets</h1>
        <p className="text-sm text-gray-600">List and create backup targets.</p>
      </div>

      <section className="rounded-md border">
        <div className="p-4 border-b font-medium">Create Target</div>
        <form
          className="p-4 grid gap-4 sm:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault()
            createMut.mutate(form)
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
          <label className="grid gap-1">
            <span className="text-sm">Slug</span>
            <input
              className="border rounded px-3 py-2"
              value={form.slug}
              onChange={(e) => setForm({ ...form, slug: e.target.value })}
              required
            />
          </label>
          <label className="grid gap-1">
            <span className="text-sm">Type</span>
            <input
              className="border rounded px-3 py-2"
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}
              placeholder="postgres | pihole | ..."
              required
            />
          </label>
          <label className="grid gap-1 sm:col-span-2">
            <span className="text-sm">Config JSON</span>
            <textarea
              className="border rounded px-3 py-2 h-28 font-mono text-sm"
              value={form.config_json}
              onChange={(e) => setForm({ ...form, config_json: e.target.value })}
            />
          </label>
          <div className="sm:col-span-2">
            <Button type="submit" disabled={createMut.isPending}>
              {createMut.isPending ? 'Creating...' : 'Create'}
            </Button>
            {createMut.error && (
              <span className="ml-3 text-sm text-red-600">{String(createMut.error)}</span>
            )}
          </div>
        </form>
      </section>

      <section className="rounded-md border overflow-x-auto">
        <div className="p-4 border-b font-medium">Existing Targets</div>
        {isLoading ? (
          <div className="p-4 text-sm text-gray-600">Loading...</div>
        ) : error ? (
          <div className="p-4 text-sm text-red-600">{String(error)}</div>
        ) : (
          <table className="min-w-full text-sm">
            <thead className="bg-muted/50 text-left">
              <tr>
                <th className="px-4 py-2">Name</th>
                <th className="px-4 py-2">Type</th>
                <th className="px-4 py-2">Has Schedule?</th>
                <th className="px-4 py-2">Created</th>
              </tr>
            </thead>
            <tbody>
              {(targets ?? []).map((t: Target) => (
                <tr key={t.id} className="border-t">
                  <td className="px-4 py-2">{t.name}</td>
                  <td className="px-4 py-2">{t.type}</td>
                  <td className="px-4 py-2">{/* schedule presence unknown from API yet */}—</td>
                  <td className="px-4 py-2">
                    {new Date(t.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}


