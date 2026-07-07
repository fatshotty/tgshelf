// Modal folder chooser for move/copy. Navigates the ACTIVE folder tree by node id
// (folders only), starting at root via resolve('/'); reuses the ['children', id]
// cache. "Pick this folder" returns the folder currently open in the picker. The
// backend rejects invalid targets (e.g. into self/descendant) → surfaced as a toast
// by the caller.
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '../api/client'
import { FolderIcon } from './Icons'
import { Modal } from './Modal'

export function FolderPicker({
  title,
  onClose,
  onPick,
}: {
  title: string
  onClose: () => void
  onPick: (folderId: string) => void
}) {
  const rootQ = useQuery({ queryKey: ['resolve', '/'], queryFn: () => api.resolve('/') })
  const [stack, setStack] = useState<{ id: string; name: string }[]>([]) // breadcrumb below root
  const folderId = stack.length ? stack[stack.length - 1].id : rootQ.data?.id

  const childrenQ = useQuery({
    queryKey: ['children', folderId],
    queryFn: () => api.listChildren(folderId!),
    enabled: !!folderId,
  })
  const folders = (childrenQ.data ?? []).filter((n) => n.is_folder)

  return (
    <Modal title={title} onClose={onClose}>
      <div className="picker">
        <div className="picker-crumbs">
          <button className="crumb" onClick={() => setStack([])}>
            root
          </button>
          {stack.map((c, i) => (
            <span className="crumbseg" key={c.id}>
              <span className="sep">/</span>
              <button className="crumb" onClick={() => setStack(stack.slice(0, i + 1))}>
                {c.name}
              </button>
            </span>
          ))}
        </div>
        <div className="picker-list">
          {folders.length === 0 ? (
            <div className="empty">no subfolders</div>
          ) : (
            folders.map((f) => (
              <button
                key={f.id}
                className="picker-row"
                onClick={() => setStack([...stack, { id: f.id, name: f.name }])}
              >
                <FolderIcon className="ic" />
                <span>{f.name}</span>
              </button>
            ))
          )}
        </div>
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button disabled={!folderId} onClick={() => folderId && onPick(folderId)}>
            Pick this folder
          </button>
        </div>
      </div>
    </Modal>
  )
}
