import { Virtuoso } from 'react-virtuoso'

import { downloadUrl } from '../api/client'
import type { FsNode } from '../api/types'
import { humanBytes } from '../lib/format'
import { NodeMenu, type NodeAction } from './NodeMenu'

// ACTIVE before DELETED, then folders before files, then by name (numeric-aware).
function ordering(a: FsNode, b: FsNode): number {
  if (a.state !== b.state) return a.state === 'DELETED' ? 1 : -1
  if (a.is_folder !== b.is_folder) return a.is_folder ? -1 : 1
  return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
}

// Virtualized list (react-virtuoso) → stays fast on folders with many thousands of
// entries. Each row carries a ⋯ actions menu; DELETED rows are dimmed.
export function NodeList({
  nodes,
  onOpenFolder,
  onAction,
}: {
  nodes: FsNode[]
  onOpenFolder: (name: string) => void
  onAction: (node: FsNode, a: NodeAction) => void
}) {
  const sorted = [...nodes].sort(ordering)
  if (sorted.length === 0) return <div className="empty">Empty folder</div>
  return (
    <Virtuoso
      className="nodelist"
      data={sorted}
      itemContent={(_, node) => {
        const deleted = node.state === 'DELETED'
        return (
          <div className={`row ${node.is_folder ? 'folder' : 'file'}${deleted ? ' deleted' : ''}`}>
            {node.is_folder ? (
              <button
                className="rowmain"
                onClick={() => !deleted && onOpenFolder(node.name)}
                disabled={deleted}
              >
                <span className="ic">📁</span>
                <span className="name">{node.name}</span>
              </button>
            ) : (
              <div className="rowmain">
                <span className="ic">📄</span>
                <span className="name">{node.name}</span>
                <span className="meta">
                  {humanBytes(node.size)}
                  {node.mime ? ` · ${node.mime}` : ''}
                </span>
              </div>
            )}
            {!deleted && !node.is_folder ? (
              <a className="dl" href={downloadUrl(node.id, node.name)} target="_blank" rel="noreferrer">
                ↓
              </a>
            ) : null}
            <NodeMenu node={node} onAction={(a) => onAction(node, a)} />
          </div>
        )
      }}
    />
  )
}
