import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type Run } from '../api/client'

export default function RunsPage() {
  const { data, isLoading, error } = useQuery({ queryKey: ['runs'], queryFn: api.listRuns })
  const runs = useMemo(() => (data ?? []).slice(-20).reverse(), [data])

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">Runs</h1>
        <p className="text-sm text-gray-600">Last 20 runs.</p>
      </div>

      <section className="rounded-md border overflow-x-auto">
        <table className="min-w-full text-sm">
          <thead className="bg-muted/50 text-left">
            <tr>
              <th className="px-4 py-2">ID</th>
              <th className="px-4 py-2">Status</th>
              <th className="px-4 py-2">Started</th>
              <th className="px-4 py-2">Finished</th>
              <th className="px-4 py-2">Artifact</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr><td className="px-4 py-3" colSpan={5}>Loading...</td></tr>
            ) : error ? (
              <tr><td className="px-4 py-3 text-red-600" colSpan={5}>{String(error)}</td></tr>
            ) : runs.length === 0 ? (
              <tr><td className="px-4 py-3" colSpan={5}>No runs yet.</td></tr>
            ) : (
              runs.map((r: Run) => (
                <tr key={r.id} className="border-t">
                  <td className="px-4 py-2">{r.id}</td>
                  <td className="px-4 py-2">
                    <span className={
                      r.status === 'success' ? 'text-green-600' : r.status === 'failed' ? 'text-red-600' : 'text-gray-700'
                    }>
                      {r.status}
                    </span>
                  </td>
                  <td className="px-4 py-2">{new Date(r.started_at).toLocaleString()}</td>
                  <td className="px-4 py-2">{r.finished_at ? new Date(r.finished_at).toLocaleString() : '—'}</td>
                  <td className="px-4 py-2">
                    {r.artifact_path ? (
                      <a className="text-blue-600 hover:underline" href={r.artifact_path} target="_blank" rel="noreferrer">Artifact</a>
                    ) : '—'}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </section>
    </div>
  )
}


