import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { api, ApiError } from '../api/client'
import type { FsNode } from '../api/types'
import { ConfirmDialog, PromptDialog, SizeDialog } from '../components/dialogs'
import { FileEditDialog } from '../components/FileEditDialog'
import { FolderPicker } from '../components/FolderPicker'
import { NodeList } from '../components/NodeList'
import type { NodeAction } from '../components/NodeMenu'
import { useToast } from '../components/Toast'
import { useTreeActions } from '../lib/mutations'
import { browseUrl, fsPath, pathSegments } from '../lib/path'

// which modal (if any) is open, and on which node
type Dialog =
  | { kind: 'newFolder' }
  | { kind: 'edit'; node: FsNode }
  | { kind: 'rename'; node: FsNode }
  | { kind: 'setChannel'; node: FsNode }
  | { kind: 'delete'; node: FsNode }
  | { kind: 'purge'; node: FsNode }
  | { kind: 'move'; node: FsNode }
  | { kind: 'copy'; node: FsNode }
  | { kind: 'size'; node: FsNode }
  | null

export default function BrowseView() {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const toast = useToast()
  const segments = pathSegments(pathname)
  const path = fsPath(segments)

  const folderQ = useQuery({ queryKey: ['resolve', path], queryFn: () => api.resolve(path) })
  const folder = folderQ.data

  const activeQ = useQuery({
    queryKey: ['children', folder?.id],
    queryFn: () => api.listChildren(folder!.id),
    enabled: !!folder?.id,
  })
  const [showDeleted, setShowDeleted] = useState(false)
  const deletedQ = useQuery({
    queryKey: ['children', folder?.id, 'DELETED'],
    queryFn: () => api.listChildren(folder!.id, 'DELETED'),
    enabled: !!folder?.id && showDeleted,
  })
  const nodes: FsNode[] = [...(activeQ.data ?? []), ...(showDeleted ? deletedQ.data ?? [] : [])]

  const actions = useTreeActions(folder?.id, path)
  const [dialog, setDialog] = useState<Dialog>(null)

  const onAction = (node: FsNode, a: NodeAction) => {
    if (a === 'size') setDialog({ kind: 'size', node })
    else if (a === 'edit') setDialog({ kind: 'edit', node })
    else if (a === 'rename') setDialog({ kind: 'rename', node })
    else if (a === 'setChannel') setDialog({ kind: 'setChannel', node })
    else if (a === 'move') setDialog({ kind: 'move', node })
    else if (a === 'copy') setDialog({ kind: 'copy', node })
    else if (a === 'delete') setDialog({ kind: 'delete', node })
    else if (a === 'purge') setDialog({ kind: 'purge', node })
    else if (a === 'restore') actions.restore(node.id).catch((e) => toast(String(e), 'error'))
  }

  const [q, setQ] = useState('')
  const onSearch = (e: FormEvent) => {
    e.preventDefault()
    if (q.trim()) navigate(`/search?q=${encodeURIComponent(q.trim())}`)
  }

  return (
    <div className="browse">
      <div className="bar">
        <nav className="crumbs">
          <button className="crumb" onClick={() => navigate(browseUrl([]))}>
            root
          </button>
          {segments.map((seg, i) => (
            <span className="crumbseg" key={i}>
              <span className="sep">/</span>
              <button className="crumb" onClick={() => navigate(browseUrl(segments.slice(0, i + 1)))}>
                {seg}
              </button>
            </span>
          ))}
        </nav>
        <div className="bar-actions">
          <button className="btn" disabled={!folder?.is_folder} onClick={() => setDialog({ kind: 'newFolder' })}>
            New folder
          </button>
          <label className="toggle">
            <input type="checkbox" checked={showDeleted} onChange={(e) => setShowDeleted(e.target.checked)} />
            Show deleted
          </label>
        </div>
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
        {folder?.is_folder && activeQ.isLoading && <div className="info">Loading…</div>}
        {folder?.is_folder && activeQ.data && (
          <NodeList nodes={nodes} onOpenFolder={(name) => navigate(browseUrl([...segments, name]))} onAction={onAction} />
        )}
      </div>

      {dialog?.kind === 'newFolder' && (
        <PromptDialog
          title="New folder"
          label="Folder name"
          placeholder="name"
          onClose={() => setDialog(null)}
          onSubmit={(name) => actions.createFolder(name)}
        />
      )}
      {dialog?.kind === 'size' && (
        <SizeDialog nodeId={dialog.node.id} name={dialog.node.name} onClose={() => setDialog(null)} />
      )}
      {dialog?.kind === 'edit' && (
        <FileEditDialog
          node={dialog.node}
          onClose={() => setDialog(null)}
          onSave={(data, force) => actions.setContent(dialog.node.id, data, force)}
        />
      )}
      {dialog?.kind === 'rename' && (
        <PromptDialog
          title="Rename"
          label="New name"
          initial={dialog.node.name}
          onClose={() => setDialog(null)}
          onSubmit={(name) => actions.rename(dialog.node.id, name)}
        />
      )}
      {dialog?.kind === 'setChannel' && (
        <PromptDialog
          title="Set channel"
          label="Channel id (empty = inherit from parent)"
          initial={dialog.node.channel_id != null ? String(dialog.node.channel_id) : ''}
          placeholder="-100…"
          onClose={() => setDialog(null)}
          onSubmit={(v) => actions.setChannel(dialog.node.id, v.trim() === '' ? null : Number(v))}
        />
      )}
      {dialog?.kind === 'delete' && (
        <ConfirmDialog
          title="Delete"
          message={`Delete "${dialog.node.name}"? It can be restored from "Show deleted".`}
          confirmLabel="Delete"
          danger
          onClose={() => setDialog(null)}
          onConfirm={() => actions.remove(dialog.node.id, false).catch((e) => toast(String(e), 'error'))}
        />
      )}
      {dialog?.kind === 'purge' && (
        <ConfirmDialog
          title="Purge"
          message={`Permanently delete "${dialog.node.name}"? This cannot be undone.`}
          confirmLabel="Purge"
          danger
          onClose={() => setDialog(null)}
          onConfirm={() => actions.remove(dialog.node.id, true).catch((e) => toast(String(e), 'error'))}
        />
      )}
      {(dialog?.kind === 'move' || dialog?.kind === 'copy') && (
        <FolderPicker
          title={dialog.kind === 'move' ? `Move "${dialog.node.name}" to…` : `Copy "${dialog.node.name}" to…`}
          onClose={() => setDialog(null)}
          onPick={(destId) => {
            const node = dialog.node
            const op = dialog.kind === 'move' ? actions.move : actions.copy
            setDialog(null)
            op(node.id, destId).catch((e) => toast(String(e), 'error'))
          }}
        />
      )}
    </div>
  )
}
