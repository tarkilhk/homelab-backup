import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Tag, type TagTargetAttachment, type Group } from '../api/client'
import AppCard from '../components/ui/AppCard'
import IconButton from '../components/IconButton'
import { Button } from '../components/ui/button'
import { useConfirm } from '../components/ConfirmProvider'
import { Plus, Trash2 } from 'lucide-react'
import { formatLocalDateTime } from '../lib/dates'

export default function TagsPage() {
  const confirm = useConfirm()
  const qc = useQueryClient()

  const { data: tags } = useQuery({ queryKey: ['tags'], queryFn: api.listTags })
  const { data: targets } = useQuery({ queryKey: ['targets'], queryFn: api.listTargets })
  const { data: groups } = useQuery({ queryKey: ['groups'], queryFn: api.listGroups })

  const [selectedTagId, setSelectedTagId] = useState<number | ''>('')

  // Auto-select first tag when available
  useEffect(() => {
    if (selectedTagId === '' && Array.isArray(tags) && tags.length > 0) {
      setSelectedTagId(tags[0].id)
    }
  }, [tags, selectedTagId])

  const { data: attachments } = useQuery({
    queryKey: ['tags', selectedTagId, 'targets'],
    queryFn: () => api.listTargetsForTag(selectedTagId as number),
    enabled: Number.isFinite(selectedTagId as number),
  })

  const [originFilter, setOriginFilter] = useState<'' | 'AUTO' | 'DIRECT' | 'GROUP'>('')

  const filteredAttachments = useMemo(() => {
    const list = (attachments ?? []) as TagTargetAttachment[]
    if (!originFilter) return list
    return list.filter((a) => a.origin === originFilter)
  }, [attachments, originFilter])

  const deleteMut = useMutation({
    mutationFn: (id: number) => api.deleteTag(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] })
      // If we deleted the currently selected tag, clear selection
      if (Number.isFinite(selectedTagId as number) && tags?.some((t) => t.id === selectedTagId) === false) {
        setSelectedTagId('')
      }
    },
  })

  // Create tag form state
  const [showEditor, setShowEditor] = useState(false)
  const [form, setForm] = useState<{ name: string }>({ name: '' })
  const createMut = useMutation({
    mutationFn: (payload: { name: string }) => api.createTag(payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['tags'] })
      setForm({ name: '' })
      setShowEditor(false)
    },
  })

  // Determine auto-tags by comparing tag.slug to existing target slugs and slugified group names
  const autoTagSlugSet = useMemo(() => new Set(((targets ?? []) as { slug: string }[]).map((t) => t.slug)), [targets])

  function slugify(value: string): string {
    const s = value.trim().toLowerCase()
    let out = ''
    let prevDash = false
    for (const ch of s) {
      if (/^[a-z0-9]$/i.test(ch)) {
        out += ch.toLowerCase()
        prevDash = false
      } else if (ch === '-' || ch === '_') {
        out += ch
        prevDash = false
      } else {
        if (!prevDash) {
          out += '-'
          prevDash = true
        }
      }
    }
    out = out.replace(/^-+|-+$/g, '')
    return out || 'item'
  }

  const groupAutoSlugSet = useMemo(() => new Set(((groups ?? []) as Group[]).map((g) => slugify(g.name))), [groups])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Tags</h1>
          <p className="text-sm text-muted-foreground">View tags and see which targets they apply to.</p>
        </div>
        <IconButton
          variant="accent"
          aria-label="Add Tag"
          onClick={() => setShowEditor(true)}
        >
          <Plus className="h-4 w-4" aria-hidden="true" /> Add Tag
        </IconButton>
      </div>

      {showEditor && (
        <AppCard title={"Create Tag"}>
          <form
            className="grid gap-4 sm:grid-cols-2"
            onSubmit={(e) => {
              e.preventDefault()
              if (!form.name.trim()) return
              createMut.mutate({ name: form.name.trim() })
            }}
          >
            <label className="grid gap-1">
              <span className="text-sm">Name</span>
              <input
                className="border rounded px-3 py-2"
                value={form.name}
                onChange={(e) => setForm({ name: e.target.value })}
                required
              />
            </label>
            <div className="sm:col-span-2 flex items-center gap-2">
              <Button type="submit" disabled={createMut.isPending}>
                {createMut.isPending ? 'Creating…' : 'Create Tag'}
              </Button>
              <Button type="button" variant="cancel" onClick={() => { setShowEditor(false); setForm({ name: '' }) }}>
                Cancel
              </Button>
            </div>
          </form>
        </AppCard>
      )}

      <AppCard title="Existing Tags" className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-muted/50 text-left">
            <tr>
              <th className="px-4 py-2">Name</th>
              <th className="px-4 py-2">Slug</th>
              <th className="px-4 py-2">Created</th>
              <th className="px-4 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(tags ?? []).map((t: Tag) => (
              <tr key={t.id} className="border-t align-top">
                <td className="px-4 py-2">{t.display_name}</td>
                <td className="px-4 py-2 font-mono">{t.slug}</td>
                <td className="px-4 py-2">{formatLocalDateTime(t.created_at)}</td>
                <td className="px-4 py-2 text-right">
                  <div className="flex justify-end gap-2">
                    {!(autoTagSlugSet.has(t.slug) || groupAutoSlugSet.has(t.slug)) && (
                      <button
                        aria-label={`Delete tag ${t.display_name}`}
                        className="p-2 rounded hover:bg-muted"
                        onClick={async () => {
                          const ok = await confirm({
                            title: `dev.tarkilnetwork:8081`,
                            description: `Delete tag "${t.display_name}"? This cannot be undone.`,
                            confirmText: 'OK',
                            cancelText: 'Cancel',
                            variant: 'danger',
                          })
                          if (ok) deleteMut.mutate(t.id)
                        }}
                      >
                        <Trash2 className="h-4 w-4 text-red-600" />
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </AppCard>

      <AppCard title="Tag Attachments">
        <div className="grid gap-4 md:grid-cols-3">
          <label className="grid gap-1 md:col-span-1">
            <span className="text-sm">Tag</span>
            <select
              className="border rounded px-3 py-2 bg-background"
              value={selectedTagId === '' ? '' : String(selectedTagId)}
              onChange={(e) => setSelectedTagId(e.target.value ? Number(e.target.value) : '')}
              aria-label="Select Tag"
            >
              <option value="">Select a tag…</option>
              {(tags ?? []).map((t: Tag) => (
                <option key={t.id} value={t.id}>{t.display_name}</option>
              ))}
            </select>
          </label>
          <label className="grid gap-1 md:col-span-1">
            <span className="text-sm">Origin</span>
            <select
              className="border rounded px-3 py-2 bg-background"
              value={originFilter}
              onChange={(e) => setOriginFilter(e.target.value as any)}
              aria-label="Origin"
            >
              <option value="">All</option>
              <option value="AUTO">AUTO</option>
              <option value="DIRECT">DIRECT</option>
              <option value="GROUP">GROUP</option>
            </select>
          </label>
        </div>

        <div className="mt-4 border rounded">
          {filteredAttachments.length > 0 ? (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left">
                  <th className="px-4 py-2">Target</th>
                  <th className="px-4 py-2">Origin</th>
                  <th className="px-4 py-2">Source Group</th>
                </tr>
              </thead>
              <tbody>
                {filteredAttachments.map((a) => (
                  <tr key={`${a.target.id}-${a.origin}-${a.source_group_id ?? 'none'}`} className="border-t">
                    <td className="px-4 py-2">{a.target.name}</td>
                    <td className="px-4 py-2">
                      <span className="inline-flex items-center rounded-full border px-2 py-0.5 text-xs">{a.origin}</span>
                    </td>
                    <td className="px-4 py-2">{a.origin === 'GROUP' ? (a.source_group_id ?? '—') : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="p-4 text-sm text-muted-foreground">No targets</div>
          )}
        </div>
      </AppCard>
    </div>
  )
}


