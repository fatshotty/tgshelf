import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'

import { api, downloadUrl } from '../api/client'
import { humanBytes } from '../lib/format'

export default function SearchView() {
  const [sp] = useSearchParams()
  const q = sp.get('q') ?? ''
  const { data, isLoading, error } = useQuery({
    queryKey: ['search', q],
    queryFn: () => api.search(q),
    enabled: q.length > 0,
  })

  return (
    <div className="browse">
      <div className="bar">
        <Link className="crumb" to="/b">← back</Link>
        <span className="searchlabel">Results for “{q}”</span>
      </div>
      <div className="listwrap">
        {isLoading && <div className="info">Searching…</div>}
        {error && <div className="error">{String(error)}</div>}
        {data && data.length === 0 && <div className="empty">No results</div>}
        {data && data.length > 0 && (
          <div className="results">
            {data.map((n) => (
              <div className="row file" key={n.id}>
                <span className="ic">{n.is_folder ? '📁' : '📄'}</span>
                <span className="name">{n.name}</span>
                <span className="meta">{n.is_folder ? 'folder' : humanBytes(n.size)}</span>
                {!n.is_folder && (
                  <a className="dl" href={downloadUrl(n.id, n.name)} target="_blank" rel="noreferrer">
                    ↓
                  </a>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
