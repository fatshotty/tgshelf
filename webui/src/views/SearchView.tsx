import { useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { api, downloadUrl } from '../api/client'
import type { SearchResult } from '../api/types'
import { DownloadIcon, FileIcon, FolderIcon } from '../components/Icons'
import { ConfirmDialog } from '../components/dialogs'
import { FolderPicker } from '../components/FolderPicker'
import { useToast } from '../components/Toast'
import { humanBytes } from '../lib/format'
import { browseUrl, pathSegments } from '../lib/path'

function searchOrdering(a: SearchResult, b: SearchResult): number {
  if (a.node.is_folder !== b.node.is_folder) return a.node.is_folder ? -1 : 1
  return a.path.localeCompare(b.path, undefined, { numeric: true, sensitivity: 'base' })
}

function browsePathUrl(path: string): string {
  return browseUrl(pathSegments(path))
}

export default function SearchView() {
  const [sp] = useSearchParams()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const toast = useToast()
  const q = sp.get('q') ?? ''
  const [query, setQuery] = useState(q)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [dialog, setDialog] = useState<{ kind: 'move' | 'delete'; nodes: SearchResult[] } | null>(null)

  useEffect(() => {
    setQuery(q)
    setSelectedIds(new Set())
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
  const selected = (data ?? []).filter((result) => selectedIds.has(result.node.id) && result.node.state === 'ACTIVE')
  const visibleActiveIds = (data ?? []).filter((result) => result.node.state === 'ACTIVE').map((result) => result.node.id)
  const createJob = async (operation: 'move' | 'delete', nodes: SearchResult[], parentId?: string) => {
    const job = await api.createJob({ operation, node_ids: nodes.map((result) => result.node.id), parent_id: parentId })
    setSelectedIds(new Set())
    await Promise.all([
      qc.invalidateQueries({ queryKey: ['children'] }),
      qc.invalidateQueries({ queryKey: ['resolve'] }),
      qc.invalidateQueries({ queryKey: ['search'] }),
    ])
    toast(`Operation ${job.id} created`, 'info')
    navigate('/operations')
  }

  return (
    <div className="browse">
      <div className="bar">
        <Link className="crumb" to="/">← back</Link>
        <span className="searchlabel">Results for “{q}”</span>
        <button className="btn" disabled={visibleActiveIds.length === 0} onClick={() => setSelectedIds(new Set(visibleActiveIds))}>Select all</button>
        <button className="btn" disabled={selected.length === 0} onClick={() => setSelectedIds(new Set())}>Clear selection</button>
        <button className="btn" disabled={selected.length === 0} onClick={() => setDialog({ kind: 'move', nodes: selected })}>Move selected</button>
        <button className="btn danger" disabled={selected.length === 0} onClick={() => setDialog({ kind: 'delete', nodes: selected })}>Delete selected</button>
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
                {result.node.state === 'ACTIVE' ? (
                  <input
                    className="row-select"
                    type="checkbox"
                    checked={selectedIds.has(result.node.id)}
                    onChange={(e) => setSelectedIds((prev) => {
                      const next = new Set(prev)
                      if (e.target.checked) next.add(result.node.id)
                      else next.delete(result.node.id)
                      return next
                    })}
                  />
                ) : <span className="row-select-spacer" />}
                {result.node.is_folder ? <FolderIcon className="ic" /> : <FileIcon className="ic" />}
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
                {result.node.state === 'ACTIVE' ? <button className="btn small" onClick={() => setDialog({ kind: 'move', nodes: [result] })}>Move</button> : null}
                {result.node.state === 'ACTIVE' ? <button className="btn small danger" onClick={() => setDialog({ kind: 'delete', nodes: [result] })}>Delete</button> : null}
                {!result.node.is_folder && (
                  <a
                    className="dl"
                    href={downloadUrl(result.node.id, result.node.name)}
                    target="_blank"
                    rel="noreferrer"
                    aria-label={`Download ${result.node.name}`}
                  >
                    <DownloadIcon />
                  </a>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
      {dialog?.kind === 'move' ? (
        <FolderPicker
          title={`Move ${dialog.nodes.length} item(s) to…`}
          onClose={() => setDialog(null)}
          onPick={(parentId) => {
            const nodes = dialog.nodes
            setDialog(null)
            createJob('move', nodes, parentId).catch((e) => toast(String(e), 'error'))
          }}
        />
      ) : null}
      {dialog?.kind === 'delete' ? (
        <ConfirmDialog
          title="Delete selected"
          message={`Soft-delete ${dialog.nodes.length} item(s)? They can be restored later.`}
          confirmLabel="Delete"
          onClose={() => setDialog(null)}
          onConfirm={() => createJob('delete', dialog.nodes)}
        />
      ) : null}
    </div>
  )
}
