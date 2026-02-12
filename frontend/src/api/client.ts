export type Target = {
  id: number
  name: string
  slug: string
  // New plugin-based fields
  plugin_name?: string | null
  plugin_config_json?: string | null
  created_at: string
  updated_at: string
}

export type Tag = {
  id: number
  slug: string
  display_name: string
  created_at: string
  updated_at: string
}

export type TagCreate = {
  name: string
}

export type TargetTagWithOrigin = {
  tag: Tag
  origin: 'AUTO' | 'DIRECT' | 'GROUP'
  source_group_id?: number | null
}

export type TagTargetAttachment = {
  target: Target
  origin: 'AUTO' | 'DIRECT' | 'GROUP'
  source_group_id?: number | null
}

export type Run = {
  id: number
  job_id: number
  started_at: string
  finished_at?: string | null
  status: 'running' | 'success' | 'failed' | 'partial'
  operation?: 'backup' | 'restore'
  message?: string | null
  logs_text?: string | null
  display_job_name: string
  display_tag_name?: string | null
}

export type TargetRun = {
  id: number
  run_id: number
  target_id: number
  started_at: string
  finished_at?: string | null
  status: string
  operation?: 'backup' | 'restore'
  message?: string | null
  artifact_path?: string | null
  artifact_bytes?: number | null
  sha256?: string | null
  logs_text?: string | null
}

export type RunWithJob = Run & { job: Job; target_runs: TargetRun[] }

export type PluginInfo = {
  key: string
  name?: string
  description?: string
  version?: string
}

export type Job = {
  id: number
  tag_id: number
  name: string
  schedule_cron: string
  enabled: boolean
  retention_policy_json?: string | null
  created_at: string
  updated_at: string
}

export type JobCreate = {
  tag_id: number
  name: string
  schedule_cron: string
  enabled?: boolean
  retention_policy_json?: string | null
}

export type JobUpdate = Partial<{
  tag_id: number
  name: string
  schedule_cron: string
  enabled: boolean
  retention_policy_json: string | null
}>

// Retention policy types
export type RetentionRule = {
  unit: 'day' | 'week' | 'month' | 'year'
  window: number
  keep: number
}

export type RetentionPolicy = {
  rules: RetentionRule[]
}

export type Settings = {
  id: number
  global_retention_policy_json?: string | null
  created_at: string
  updated_at: string
}

export type SettingsUpdate = {
  global_retention_policy_json?: string | null
}

export type RetentionPreviewResult = {
  keep_count: number
  delete_count: number
  deleted_paths: string[]
  kept_paths: string[]
}

// Maintenance types
export type MaintenanceJob = {
  id: number
  key: string
  job_type: 'retention_cleanup' | string
  name: string
  schedule_cron: string
  enabled: boolean
  config_json: string | null
  visible_in_ui: boolean
  created_at: string
  updated_at: string
}

export type MaintenanceRunResult = {
  targets_processed?: number
  deleted_count?: number
  kept_count?: number
  deleted_paths?: string[]
  error?: string
}

export type MaintenanceRun = {
  id: number
  maintenance_job_id: number
  started_at: string
  finished_at: string | null
  status: 'running' | 'success' | 'failed'
  message: string | null
  result: MaintenanceRunResult | null
  job?: MaintenanceJob
}

// Groups API types
export type Group = {
  id: number
  name: string
  description?: string | null
  created_at: string
  updated_at: string
}

export type GroupCreate = {
  name: string
  description?: string | null
}

export type GroupUpdate = Partial<GroupCreate>

export type GroupWithTargets = Group & { targets: Target[] }
export type GroupWithTags = Group & { tags: Tag[] }

const API_BASE = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let errorMessage = `Request failed ${res.status}`
    const contentType = res.headers.get('content-type') ?? ''
    try {
      if (contentType.includes('application/json')) {
        const data = await res.json()
        const detail = (data as any)?.detail ?? (data as any)?.message ?? (data as any)?.error
        if (typeof detail === 'string') {
          errorMessage = detail
        } else if (Array.isArray(detail)) {
          errorMessage = detail
            .map((d: any) => (typeof d === 'string' ? d : d?.msg ?? JSON.stringify(d)))
            .join(', ')
        } else {
          errorMessage = JSON.stringify(data)
        }
      } else {
        const text = await res.text()
        errorMessage = text || errorMessage
      }
    } catch {
      // Swallow parsing errors and fall back to generic message
    }
    const err = new Error(errorMessage)
    ;(err as any).status = res.status
    throw err
  }
  if (res.status === 204) return undefined as unknown as T
  return (await res.json()) as T
}

type TargetCreatePlugin = { name: string; slug?: string } & {
  plugin_name: string
  plugin_config_json: string
}

export type TargetUpdate = Partial<{
  name: string
  slug?: string
  plugin_name: string
  plugin_config_json: string
}>

export const api = {
  listTargets: () => request<Target[]>('/targets/'),
  getTarget: (id: number) => request<Target>(`/targets/${id}`),
  listTargetTags: (id: number) => request<TargetTagWithOrigin[]>(`/targets/${id}/tags`),
  listTargetSchedules: (id: number) => request<string[]>(`/targets/${id}/schedules`),
  createTarget: (payload: TargetCreatePlugin) =>
    request<Target>('/targets/', { method: 'POST', body: JSON.stringify(payload) }),
  updateTarget: (id: number, payload: TargetUpdate) =>
    request<Target>(`/targets/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteTarget: (id: number) => request<void>(`/targets/${id}`, { method: 'DELETE' }),
  // Connectivity tests
  testPlugin: (key: string, config: Record<string, unknown>) =>
    request<{ ok: boolean; error?: string }>(`/plugins/${encodeURIComponent(key)}/test`, {
      method: 'POST',
      body: JSON.stringify(config ?? {}),
    }),
  testTarget: (id: number) =>
    request<{ ok: boolean; error?: string }>(`/targets/${id}/test`, { method: 'POST' }),
  listRuns: (params?: { status?: string; start_date?: string; end_date?: string; tag_id?: number }) => {
    const search = new URLSearchParams()
    if (params?.status) search.set('status', params.status)
    if (params?.start_date) search.set('start_date', params.start_date)
    if (params?.end_date) search.set('end_date', params.end_date)
    if (params?.tag_id != null) search.set('tag_id', String(params.tag_id))
    const q = search.toString()
    return request<RunWithJob[]>(`/runs/${q ? `?${q}` : ''}`)
  },
  getRun: (id: number) => request<RunWithJob>(`/runs/${id}`),
  listPlugins: () => request<PluginInfo[]>('/plugins/'),
  getPluginSchema: (key: string) => request<Record<string, unknown>>(`/plugins/${key}/schema`),
  // Tags
  listTags: () => request<Tag[]>('/tags/'),
  getTag: (id: number) => request<Tag>(`/tags/${id}`),
  createTag: (payload: TagCreate) => request<Tag>('/tags/', { method: 'POST', body: JSON.stringify(payload) }),
  listTargetsForTag: (id: number) => request<TagTargetAttachment[]>(`/tags/${id}/targets`),
  deleteTag: (id: number) => request<void>(`/tags/${id}`, { method: 'DELETE' }),
  // Groups
  listGroups: () => request<Group[]>('/groups/'),
  createGroup: (payload: GroupCreate) => request<Group>('/groups/', { method: 'POST', body: JSON.stringify(payload) }),
  updateGroup: (id: number, payload: GroupUpdate) => request<Group>(`/groups/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteGroup: (id: number) => request<void>(`/groups/${id}`, { method: 'DELETE' }),
  getGroupTargets: (id: number) => request<GroupWithTargets>(`/groups/${id}/targets`),
  addTargetsToGroup: (id: number, target_ids: number[]) =>
    request<GroupWithTargets>(`/groups/${id}/targets`, { method: 'POST', body: JSON.stringify({ target_ids }) }),
  removeTargetsFromGroup: (id: number, target_ids: number[]) =>
    request<GroupWithTargets>(`/groups/${id}/targets`, { method: 'DELETE', body: JSON.stringify({ target_ids }) }),
  getGroupTags: (id: number) => request<GroupWithTags>(`/groups/${id}/tags`),
  addTagsToGroup: (id: number, tag_names: string[]) =>
    request<GroupWithTags>(`/groups/${id}/tags`, { method: 'POST', body: JSON.stringify({ tag_names }) }),
  removeTagsFromGroup: (id: number, tag_names: string[]) =>
    request<GroupWithTags>(`/groups/${id}/tags`, { method: 'DELETE', body: JSON.stringify({ tag_names }) }),
  // Jobs
  listJobs: () => request<Job[]>('/jobs/'),
  createJob: (payload: JobCreate) => request<Job>('/jobs/', { method: 'POST', body: JSON.stringify(payload) }),
  getJob: (id: number) => request<Job>(`/jobs/${id}`),
  updateJob: (id: number, payload: JobUpdate) => request<Job>(`/jobs/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteJob: (id: number) => request<void>(`/jobs/${id}`, { method: 'DELETE' }),
  runJobNow: (id: number) => request<Run>(`/jobs/${id}/run`, { method: 'POST' }),
  // Dashboard helpers
  upcomingJobs: () => request<Array<{ job_id: number; name: string; next_run_at: string }>>('/jobs/upcoming'),
  // Restores
  restoreTargetRun: (payload: { artifact_path: string; destination_target_id: number; source_target_run_id?: number; triggered_by?: string }) =>
    request<RunWithJob>('/restores/', { method: 'POST', body: JSON.stringify(payload) }),
  // Available backups
  listBackupsFromDisk: () => request<Array<{
    artifact_path: string
    target_slug: string | null
    date: string | null
    plugin_name: string | null
    file_size: number
    modified_at: string
    metadata_source: 'sidecar' | 'inferred'
  }>>('/backups/from-disk'),
  // Settings
  getSettings: () => request<Settings>('/settings/'),
  updateSettings: (payload: SettingsUpdate) =>
    request<Settings>('/settings/', { method: 'PUT', body: JSON.stringify(payload) }),
  // Retention
  previewRetention: (jobId: number, targetId: number) =>
    request<RetentionPreviewResult>(`/settings/retention/preview?job_id=${jobId}&target_id=${targetId}`, { method: 'POST' }),
  runRetention: (jobId?: number, targetId?: number) => {
    const params = new URLSearchParams()
    if (jobId != null) params.set('job_id', String(jobId))
    if (targetId != null) params.set('target_id', String(targetId))
    const q = params.toString()
    return request<RetentionPreviewResult>(`/settings/retention/run${q ? `?${q}` : ''}`, { method: 'POST' })
  },
  // Maintenance
  listMaintenanceJobs: (visibleInUi?: boolean) => {
    const params = new URLSearchParams()
    if (visibleInUi !== undefined) params.set('visible_in_ui', String(visibleInUi))
    const q = params.toString()
    return request<MaintenanceJob[]>(`/maintenance/jobs${q ? `?${q}` : ''}`)
  },
  getMaintenanceJob: (id: number) => request<MaintenanceJob>(`/maintenance/jobs/${id}`),
  listMaintenanceRuns: (limit?: number) => {
    const params = new URLSearchParams()
    if (limit != null) params.set('limit', String(limit))
    const q = params.toString()
    return request<MaintenanceRun[]>(`/maintenance/runs${q ? `?${q}` : ''}`)
  },
  getMaintenanceRun: (id: number) => request<MaintenanceRun>(`/maintenance/runs/${id}`),
}
