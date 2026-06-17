import { Virtuoso } from 'react-virtuoso'

import { downloadUrl } from '../api/client'
import type { FsNode } from '../api/types'
import { humanBytes } from '../lib/format'

function byFolderThenName(a: FsNode, b: FsNode): number {
  if (a.is_folder !== b.is_folder) return a.is_folder ? -1 : 1
  return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
}

// Virtualized list (react-virtuoso) → stays fast on folders with many thousands
// of entries. Folders first, then by name.
export function NodeList({
  nodes,
  onOpenFolder,
}: {
  nodes: FsNode[]
  onOpenFolder: (name: string) => void
}) {
  const sorted = [...nodes].sort(byFolderThenName)
  if (sorted.length === 0) return <div className="empty">Empty folder</div>
  return (
    <Virtuoso
      className="nodelist"
      data={sorted}
      itemContent={(_, node) =>
        node.is_folder ? (
          <button className="row folder" onClick={() => onOpenFolder(node.name)}>
            <span className="ic">📁</span>
            <span className="name">{node.name}</span>
          </button>
        ) : (
          <div className="row file">
            <span className="ic">📄</span>
            <span className="name">{node.name}</span>
            <span className="meta">
              {humanBytes(node.size)}
              {node.mime ? ` · ${node.mime}` : ''}
            </span>
            <a className="dl" href={downloadUrl(node.id, node.name)} target="_blank" rel="noreferrer">
              ↓
            </a>
          </div>
        )
      }
    />
  )
}
