import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api, downloadUrl } from '../api/client'
import type { SearchResult } from '../api/types'
import { humanBytes } from '../lib/format'
import { browseUrl, pathSegments } from '../lib/path'

function searchOrdering(a: SearchResult, b: SearchResult): number {
  if (a.node.is_folder !== b.node.is_folder) return a.node.is_folder ? -1 : 1
  return a.path.localeCompare(b.path, undefined, { numeric: true, sensitivity: 'base' })
}

function browsePathUrl(path: string): string {
  return browseUrl(pathSegments(`/b${path}`))
}

export default function SearchView() {
  const [sp] = useSearchParams()
  const navigate = useNavigate()
  const q = sp.get('q') ?? ''
  const [query, setQuery] = useState(q)

  useEffect(() => {
    setQuery(q)
  }, [q])

  const onSearch = (e: FormEvent) => {
    e.preventDefault()
    const term = query.trim()
    if (term) navigate(`/search?q=${encodeURIComponent(term)}`)
  }

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
        <form className="search" onSubmit={onSearch}>
          <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search…" />
          <button className="btn" type="submit" disabled={!query.trim()}>
            Search
          </button>
        </form>
      </div>
      <div className="listwrap">
        {isLoading && <div className="info">Searching…</div>}
        {error && <div className="error">{String(error)}</div>}
        {data && data.length === 0 && <div className="empty">No results</div>}
        {data && data.length > 0 && (
          <div className="results">
            {[...data].sort(searchOrdering).map((result) => (
              <div className={`row ${result.node.is_folder ? 'folder' : 'file'}`} key={result.node.id}>
                <span className="ic">{result.node.is_folder ? '📁' : '📄'}</span>
                <span className="search-result-main">
                  <span className="name">{result.node.name}</span>
                  <span className="path">{result.node.is_folder ? result.path : result.parent_path}</span>
                </span>
                <span className="meta">{result.node.is_folder ? 'folder' : humanBytes(result.node.size)}</span>
                {result.node.is_folder ? (
                  <Link className="btn small" to={browsePathUrl(result.path)}>
                    Open
                  </Link>
                ) : (
                  <Link className="btn small" to={browsePathUrl(result.parent_path)}>
                    Open folder
                  </Link>
                )}
                {!result.node.is_folder && (
                  <a className="dl" href={downloadUrl(result.node.id, result.node.name)} target="_blank" rel="noreferrer">
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
