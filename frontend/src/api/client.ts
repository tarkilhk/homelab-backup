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

export type Run = {
  id: number
  job_id: number
  started_at: string
  finished_at?: string | null
  status: string
  message?: string | null
  artifact_path?: string | null
}

export type PluginInfo = {
  key: string
  name?: string
  description?: string
  version?: string
}

export type Job = {
  id: number
  target_id: number
  name: string
  schedule_cron: string
  enabled: string
  created_at: string
  updated_at: string
}

export type JobCreate = {
  target_id: number
  name: string
  schedule_cron: string
  enabled?: string
}

export type JobUpdate = Partial<{
  target_id: number
  name: string
  schedule_cron: string
  enabled: string
}>

const API_BASE = '/api/v1'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`Request failed ${res.status}: ${text}`)
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
  createTarget: (payload: TargetCreatePlugin) =>
    request<Target>('/targets/', { method: 'POST', body: JSON.stringify(payload) }),
  updateTarget: (id: number, payload: TargetUpdate) =>
    request<Target>(`/targets/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteTarget: (id: number) => request<void>(`/targets/${id}`, { method: 'DELETE' }),
  listRuns: () => request<Run[]>('/runs/'),
  listPlugins: () => request<PluginInfo[]>('/plugins/'),
  getPluginSchema: (key: string) => request<Record<string, unknown>>(`/plugins/${key}/schema`),
  // Jobs
  listJobs: () => request<Job[]>('/jobs/'),
  createJob: (payload: JobCreate) => request<Job>('/jobs/', { method: 'POST', body: JSON.stringify(payload) }),
  getJob: (id: number) => request<Job>(`/jobs/${id}`),
  updateJob: (id: number, payload: JobUpdate) => request<Job>(`/jobs/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  deleteJob: (id: number) => request<void>(`/jobs/${id}`, { method: 'DELETE' }),
  runJobNow: (id: number) => request<Run>(`/jobs/${id}/run`, { method: 'POST' }),
}


