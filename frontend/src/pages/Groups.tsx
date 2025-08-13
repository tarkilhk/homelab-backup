import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type Group, type GroupWithTargets, type Target } from '../api/client'
import AppCard from '../components/ui/AppCard'
import IconButton from '../components/IconButton'
import { Button } from '../components/ui/button'
import { useConfirm } from '../components/ConfirmProvider'
import MultiSelectList from '../components/MultiSelectList'
import { Plus, Pencil, Trash2 } from 'lucide-react'
import { formatLocalDateTime } from '../lib/dates'

export default function GroupsPage() {
  const confirm = useConfirm()
  const qc = useQueryClient()
  const { data: groups } = useQuery({ queryKey: ['groups'], queryFn: api.listGroups })
  const { data: targets } = useQuery({ queryKey: ['targets'], queryFn: api.listTargets })

  const [showEditor, setShowEditor] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [form, setForm] = useState<{ name: string; description: string }>({ name: '', description: '' })

  const createMut = useMutation({
    mutationFn: api.createGroup,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['groups'] })
      setShowEditor(false)
      setForm({ name: '', description: '' })
    },
  })
  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: number; body: { name?: string; description?: string | null } }) => api.updateGroup(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['groups'] })
      setEditingId(null)
      setShowEditor(false)
    },
  })
  const deleteMut = useMutation({
    mutationFn: (id: number) => api.deleteGroup(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['groups'] }),
  })

  // Selection for membership/tag management
  const [selectedGroupId, setSelectedGroupId] = useState<number | ''>('')
  const [selectedTargetIds, setSelectedTargetIds] = useState<number[]>([])
  const [selectedMemberIds, setSelectedMemberIds] = useState<number[]>([])
  const [availableFilter, setAvailableFilter] = useState('')
  const [membersFilter, setMembersFilter] = useState('')
  const [workingMemberIds, setWorkingMemberIds] = useState<number[]>([])
  

  // Auto-select first group when available
  useEffect(() => {
    if (selectedGroupId === '' && Array.isArray(groups) && groups.length > 0) {
      setSelectedGroupId(groups[0].id)
    }
  }, [groups, selectedGroupId])

  const { data: groupWithTargets } = useQuery({
    queryKey: ['group', selectedGroupId, 'targets'],
    queryFn: () => api.getGroupTargets(selectedGroupId as number),
    enabled: Number.isFinite(selectedGroupId as number),
  })
  

  const addTargetsMut = useMutation({
    mutationFn: ({ id, ids }: { id: number; ids: number[] }) => api.addTargetsToGroup(id, ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['group', selectedGroupId, 'targets'] })
      qc.invalidateQueries({ queryKey: ['targets'] })
      setSelectedTargetIds([])
    },
  })
  const removeTargetsMut = useMutation({
    mutationFn: ({ id, ids }: { id: number; ids: number[] }) => api.removeTargetsFromGroup(id, ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['group', selectedGroupId, 'targets'] })
      qc.invalidateQueries({ queryKey: ['targets'] })
      setSelectedTargetIds([])
    },
  })

  // Initialize working members whenever selected group or server members change
  useEffect(() => {
    const serverIds = ((groupWithTargets as GroupWithTargets | undefined)?.targets ?? []).map((t) => t.id)
    setWorkingMemberIds(serverIds)
    setSelectedTargetIds([])
    setSelectedMemberIds([])
  }, [selectedGroupId, (groupWithTargets as GroupWithTargets | undefined)?.targets?.map((t) => t.id).join(',')])

  // Derived lists for transfer UI (use workingMemberIds)
  const currentMembers: Target[] = (targets ?? []).filter((t) => workingMemberIds.includes(t.id))
  const availableTargets: Target[] = (targets ?? []).filter((t) => !workingMemberIds.includes(t.id))
  

  useEffect(() => {
    if (!showEditor && editingId == null) setForm({ name: '', description: '' })
  }, [showEditor, editingId])

  // When entering edit mode for a group, default the membership manager
  // to that group's id so membership edits apply to the selected group.
  useEffect(() => {
    if (editingId !== null) {
      setSelectedGroupId(editingId)
    }
  }, [editingId])

  // Cancel editing helper
  function cancelEditing(): void {
    setEditingId(null)
    setForm({ name: '', description: '' })
    setShowEditor(false)
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
          <h1 className="text-2xl font-semibold">Groups</h1>
          <p className="text-sm text-muted-foreground">Organize targets by group.</p>
        </div>
        <IconButton
          variant="accent"
          aria-label="Add Group"
          onClick={() => {
            setEditingId(null)
            setForm({ name: '', description: '' })
            setShowEditor(true)
          }}
        >
          <Plus className="h-4 w-4" aria-hidden="true" /> Add Group
        </IconButton>
      </div>

      {(showEditor || editingId !== null) && (
        <AppCard title={editingId ? 'Edit Group' : 'Create Group'}>
          <form
            className="grid gap-4 sm:grid-cols-2"
            onSubmit={async (e) => {
              e.preventDefault()
              const payload = { name: form.name, description: form.description || null }
              // If editing an existing group, persist membership diffs along with group details
              if (editingId) {
                const serverIds = ((groupWithTargets as GroupWithTargets | undefined)?.targets ?? []).map((t) => t.id)
                const serverSet = new Set(serverIds)
                const workingSet = new Set(workingMemberIds)
                const toAdd = Array.from(workingSet).filter((id) => !serverSet.has(id))
                const toRemove = Array.from(serverSet).filter((id) => !workingSet.has(id))
                const ops: Promise<any>[] = []
                if (toAdd.length > 0) ops.push(addTargetsMut.mutateAsync({ id: editingId, ids: toAdd }))
                if (toRemove.length > 0) ops.push(removeTargetsMut.mutateAsync({ id: editingId, ids: toRemove }))
                if (ops.length) await Promise.all(ops)
                updateMut.mutate({ id: editingId, body: payload })
              } else {
                createMut.mutate(payload)
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
            <label className="grid gap-1">
              <span className="text-sm">Description</span>
              <input
                className="border rounded px-3 py-2"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Optional"
              />
            </label>
            <div className="sm:col-span-2 flex items-center gap-2">
              <Button type="submit" disabled={createMut.isPending || updateMut.isPending}>
                {editingId ? (updateMut.isPending ? 'Saving…' : 'Save') : (createMut.isPending ? 'Creating…' : 'Create Group')}
              </Button>
              <Button type="button" variant="cancel" onClick={cancelEditing}>Cancel</Button>
            </div>
          </form>
        </AppCard>
      )}

      {editingId !== null && (
      <AppCard title="Manage Membership" className="overflow-x-auto">
        <div className="grid gap-4 md:grid-cols-3 items-start">
          <label className="grid gap-1 md:col-span-3">
            <span className="text-sm">Group</span>
            <select
              className="border rounded px-3 py-2 bg-background"
              value={selectedGroupId === '' ? '' : String(selectedGroupId)}
              onChange={(e) => setSelectedGroupId(e.target.value ? Number(e.target.value) : '')}
              aria-label="Group"
            >
              <option value="">Select a group…</option>
              {(groups ?? []).map((g: Group) => (
                <option key={g.id} value={g.id}>{g.name}</option>
              ))}
            </select>
          </label>

          {/* Left: Available targets */}
          <div className="grid gap-2">
            <label className="grid gap-1">
              <span className="text-sm">Available targets</span>
              <input
                className="border rounded px-3 py-2"
                placeholder="Search targets"
                value={availableFilter}
                onChange={(e) => setAvailableFilter(e.target.value)}
                aria-label="Search available targets"
              />
            </label>
            <MultiSelectList
              ariaLabel="Targets"
              className="min-h-[16rem]"
              options={availableTargets
                .filter((t) => t.name.toLowerCase().includes(availableFilter.toLowerCase()))
                .map((t) => ({ value: String(t.id), label: t.name }))}
              values={selectedTargetIds.map(String)}
              onChange={(vals) => setSelectedTargetIds(vals.map((v) => Number(v)))}
              onItemDoubleClick={(val) => {
                const id = Number(val)
                if (!Number.isFinite(id)) return
                setWorkingMemberIds((prev) => Array.from(new Set([...prev, id])))
                setSelectedTargetIds([])
              }}
            />
            {availableTargets.filter((t) => t.name.toLowerCase().includes(availableFilter.toLowerCase())).length === 0 && (
              <div className="text-xs text-muted-foreground">No available targets</div>
            )}
          </div>

          {/* Middle: actions */}
          <div className="flex flex-col items-center gap-2 self-center">
            <Button
              type="button"
              disabled={!Number.isFinite(selectedGroupId as number) || selectedTargetIds.length === 0}
              onClick={() => {
                setWorkingMemberIds((prev) => Array.from(new Set([...prev, ...selectedTargetIds])))
                setSelectedTargetIds([])
              }}
            >
              Add selected →
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={!Number.isFinite(selectedGroupId as number) || selectedMemberIds.length === 0}
              onClick={() => {
                setWorkingMemberIds((prev) => prev.filter((id) => !selectedMemberIds.includes(id)))
                setSelectedMemberIds([])
              }}
            >
              ← Remove selected
            </Button>
          </div>

          {/* Right: Members */}
          <div className="grid gap-2">
            <label className="grid gap-1">
              <span className="text-sm">Targets in group</span>
              <input
                className="border rounded px-3 py-2"
                placeholder="Search in group"
                value={membersFilter}
                onChange={(e) => setMembersFilter(e.target.value)}
                aria-label="Search members"
              />
            </label>
            <MultiSelectList
              ariaLabel="Members"
              className="min-h-[16rem]"
              options={currentMembers
                .filter((t) => t.name.toLowerCase().includes(membersFilter.toLowerCase()))
                .map((t) => ({ value: String(t.id), label: t.name }))}
              values={selectedMemberIds.map(String)}
              onChange={(vals) => setSelectedMemberIds(vals.map((v) => Number(v)))}
              onItemDoubleClick={(val) => {
                const id = Number(val)
                if (!Number.isFinite(id)) return
                setWorkingMemberIds((prev) => prev.filter((m) => m !== id))
                setSelectedMemberIds([])
              }}
            />
            {currentMembers.filter((t) => t.name.toLowerCase().includes(membersFilter.toLowerCase())).length === 0 && (
              <div className="text-xs text-muted-foreground">No targets in group</div>
            )}
          </div>
          {/* No explicit save controls; saving is handled by top Save button */}
        </div>
      </AppCard>
      )}

      <AppCard title="Existing Groups" className="overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-muted/50 text-left">
            <tr>
              <th className="px-4 py-2">Name</th>
              <th className="px-4 py-2">Description</th>
              <th className="px-4 py-2">Created</th>
              <th className="px-4 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {(groups ?? []).map((g: Group) => (
              <tr
                key={g.id}
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
                  setEditingId(g.id)
                  setForm({ name: g.name, description: g.description ?? '' })
                  setShowEditor(true)
                }}
              >
                <td className="px-4 py-2 w-[25%]">{g.name}</td>
                <td className="px-4 py-2">{g.description || '—'}</td>
                <td className="px-4 py-2">{formatLocalDateTime(g.created_at)}</td>
                <td className="px-4 py-2 text-right">
                  <div className="flex justify-end gap-2">
                    <button
                      aria-label="Edit"
                      className="p-2 rounded hover:bg-muted"
                      onClick={() => {
                        setEditingId(g.id)
                        setForm({ name: g.name, description: g.description ?? '' })
                        setShowEditor(true)
                      }}
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      aria-label="Delete"
                      className="p-2 rounded hover:bg-muted"
                      onClick={async () => {
                        const ok = await confirm({
                          title: `dev.tarkilnetwork:8081`,
                          description: `Delete group "${g.name}"? This cannot be undone.`,
                          confirmText: 'OK',
                          cancelText: 'Cancel',
                          variant: 'danger',
                        })
                        if (ok) deleteMut.mutate(g.id)
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
      </AppCard>
    </div>
  )
}


