import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import { NodeList } from '../components/NodeList'
import { browseUrl, fsPath, pathSegments } from '../lib/path'

export default function BrowseView() {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const segments = pathSegments(pathname)
  const path = fsPath(segments)

  const folderQ = useQuery({ queryKey: ['resolve', path], queryFn: () => api.resolve(path) })
  const folder = folderQ.data
  const childrenQ = useQuery({
    queryKey: ['children', folder?.id],
    queryFn: () => api.listChildren(folder!.id),
    enabled: !!folder?.id,
  })

  const [q, setQ] = useState('')
  const onSearch = (e: FormEvent) => {
    e.preventDefault()
    if (q.trim()) navigate(`/search?q=${encodeURIComponent(q.trim())}`)
  }

  return (
    <div className="browse">
      <div className="bar">
        <nav className="crumbs">
          <button className="crumb" onClick={() => navigate(browseUrl([]))}>root</button>
          {segments.map((seg, i) => (
            <span className="crumbseg" key={i}>
              <span className="sep">/</span>
              <button className="crumb" onClick={() => navigate(browseUrl(segments.slice(0, i + 1)))}>
                {seg}
              </button>
            </span>
          ))}
        </nav>
        <form className="search" onSubmit={onSearch}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search…" />
        </form>
      </div>

      <div className="listwrap">
        {folderQ.isLoading && <div className="info">Loading…</div>}
        {folderQ.error && (
          <div className="error">
            {folderQ.error instanceof ApiError && folderQ.error.status === 404
              ? 'Folder not found'
              : String(folderQ.error)}
          </div>
        )}
        {folder && !folder.is_folder && <div className="error">Not a folder</div>}
        {folder?.is_folder && childrenQ.isLoading && <div className="info">Loading…</div>}
        {folder?.is_folder && childrenQ.data && (
          <NodeList
            nodes={childrenQ.data}
            onOpenFolder={(name) => navigate(browseUrl([...segments, name]))}
          />
        )}
      </div>
    </div>
  )
}
