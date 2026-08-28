import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '../api/client'

const active = (state: string) => state === 'queued' || state === 'running'

export default function OperationsView() {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const jobsQ = useQuery({
    queryKey: ['jobs'], queryFn: () => api.listJobs(),
    refetchInterval: (query) => query.state.data?.items.some((job) => active(job.state)) ? 1000 : false,
  })
  const detailQ = useQuery({
    queryKey: ['job', selectedId], queryFn: () => api.getJob(selectedId!), enabled: !!selectedId,
    refetchInterval: (query) => query.state.data && active(query.state.data.state) ? 1000 : false,
  })
  return <div className="operations">
    <section className="panel"><h2>Operations</h2>
      {jobsQ.isLoading ? <div className="info">Loading…</div> : null}
      {jobsQ.error ? <div className="error">{String(jobsQ.error)}</div> : null}
      {jobsQ.data?.items.length === 0 ? <div className="empty">No operations</div> : null}
      {jobsQ.data?.items.map((job) => <button className="job-row" key={job.id} onClick={() => setSelectedId(job.id)}>
        <strong>{job.operation}</strong><span>{job.state}</span><span>{job.succeeded + job.failed + job.skipped}/{job.total}</span><span>{job.id}</span>
      </button>)}
    </section>
    {detailQ.data ? <section className="panel job-detail"><h3>{detailQ.data.operation} · {detailQ.data.id}</h3>
      <p>{detailQ.data.succeeded} succeeded, {detailQ.data.failed} failed, {detailQ.data.skipped} skipped</p>
      {detailQ.data.error ? <div className="error">{detailQ.data.error}</div> : null}
      {detailQ.data.items.filter((item) => item.state === 'failed' || item.state === 'skipped').map((item) =>
        <div className="job-item" key={item.position}><strong>{item.source_path ?? item.node_id}</strong><span>{item.state}</span>{item.error ? <div className="error">{item.error}</div> : null}</div>)}
    </section> : null}
  </div>
}
