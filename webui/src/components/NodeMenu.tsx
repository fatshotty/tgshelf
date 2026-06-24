// Per-row ⋯ actions menu. Lists only actions applicable to the node: ACTIVE nodes
// get rename/move/copy/delete (+ set-channel for folders); DELETED nodes get
// restore/purge. Closes on outside click. Clicks stopPropagation so they don't also
// trigger the row's open-folder handler.
import { useEffect, useRef, useState } from 'react'

import type { FsNode } from '../api/types'
import { isTextMime } from '../lib/format'

export type NodeAction =
  | 'size'
  | 'edit'
  | 'rename'
  | 'setChannel'
  | 'move'
  | 'copy'
  | 'delete'
  | 'restore'
  | 'purge'

interface Item {
  a: NodeAction
  label: string
  danger?: boolean
}

export function NodeMenu({ node, onAction }: { node: FsNode; onAction: (a: NodeAction) => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  const items: Item[] =
    node.state === 'DELETED'
      ? [
          { a: 'restore', label: 'Restore' },
          { a: 'purge', label: 'Purge', danger: true },
        ]
      : [
          ...(node.is_folder ? [{ a: 'size' as NodeAction, label: 'Disk usage' }] : []),
          // inline (DB-stored) files are editable in place; text → edit, binary → view
          ...(!node.is_folder && node.inline
            ? [{ a: 'edit' as NodeAction, label: isTextMime(node.mime) ? 'Edit' : 'View' }]
            : []),
          { a: 'rename', label: 'Rename' },
          ...(node.is_folder ? [{ a: 'setChannel' as NodeAction, label: 'Set channel' }] : []),
          { a: 'move', label: 'Move…' },
          { a: 'copy', label: 'Copy…' },
          { a: 'delete', label: 'Delete', danger: true },
        ]

  return (
    <div className="nodemenu" ref={ref}>
      <button
        className="menubtn"
        onClick={(e) => {
          e.stopPropagation()
          setOpen((o) => !o)
        }}
      >
        ⋯
      </button>
      {open ? (
        <div className="menu">
          {items.map((it) => (
            <button
              key={it.a}
              className={`menuitem${it.danger ? ' danger' : ''}`}
              onClick={(e) => {
                e.stopPropagation()
                setOpen(false)
                onAction(it.a)
              }}
            >
              {it.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  )
}
