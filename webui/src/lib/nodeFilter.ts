import type { FsNode } from '../api/types'

export function filterNodesByName(nodes: FsNode[], query: string): FsNode[] {
  const term = query.trim().toLocaleLowerCase()
  if (!term) return nodes
  return nodes.filter((node) => node.name.toLocaleLowerCase().includes(term))
}
