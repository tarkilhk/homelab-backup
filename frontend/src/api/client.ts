export type Target = {
  id: number
  name: string
  slug: string
  type: string
  config_json: string
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

export const api = {
  listTargets: () => request<Target[]>('/targets/'),
  createTarget: (payload: Pick<Target, 'name' | 'slug' | 'type' | 'config_json'>) =>
    request<Target>('/targets/', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  listRuns: () => request<Run[]>('/runs/'),
}


