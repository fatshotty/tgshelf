// Thin, framework-agnostic API client (plain fetch). The browser attaches Basic
// auth (whole-origin) automatically — see auth/credentials.ts. Kept free of React
// so it (and types.ts) can be reused by a future react-native app.
import { authHeaders } from '../auth/credentials'
import type { AcceptedOp, FilePart, FilePartRef, FsNode, MetricsSnapshot, NodeSize, SearchResult, SplitPartsResult } from './types'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

type Query = Record<string, string | number | boolean | undefined>

async function request<T>(
  method: string,
  path: string,
  opts: { body?: unknown; query?: Query } = {},
): Promise<T> {
  const url = new URL(path, window.location.origin)
  if (opts.query) {
    for (const [k, v] of Object.entries(opts.query)) {
      if (v !== undefined && v !== false) url.searchParams.set(k, String(v))
    }
  }
  const headers: Record<string, string> = { ...authHeaders() }
  const init: RequestInit = { method, headers }
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(opts.body)
  }
  const resp = await fetch(url, init)
  const text = await resp.text()
  const data = text ? JSON.parse(text) : undefined
  if (!resp.ok) throw new ApiError(resp.status, data?.error ?? resp.statusText)
  return data as T
}

const id = (s: string) => encodeURIComponent(s)

export const api = {
  // -- read / navigate
  getNode: (nodeId: string) => request<FsNode>('GET', `/api/v1/nodes/${id(nodeId)}`),
  listChildren: (nodeId: string, state?: 'ACTIVE' | 'DELETED' | 'TEMP') =>
    request<FsNode[]>('GET', `/api/v1/nodes/${id(nodeId)}/children`, { query: { state } }),
  resolve: (path: string) => request<FsNode>('GET', '/api/v1/resolve', { query: { path } }),
  nodeSize: (nodeId: string) => request<NodeSize>('GET', `/api/v1/nodes/${id(nodeId)}/size`),
  search: (q: string, root?: string) => request<SearchResult[]>('GET', '/api/v1/search', { query: { q, root } }),

  // -- tree management
  createFolder: (parentId: string, name: string) =>
    request<FsNode>('POST', '/api/v1/folders', { body: { parent_id: parentId, name } }),
  rename: (nodeId: string, name: string) =>
    request<FsNode>('PUT', `/api/v1/nodes/${id(nodeId)}`, { body: { name } }),
  setFolderChannel: (nodeId: string, channelId: number | null) =>
    request<FsNode>('PUT', `/api/v1/nodes/${id(nodeId)}`, { body: { channel_id: channelId } }),
  moveNode: (nodeId: string, parentId: string) =>
    request<FsNode | AcceptedOp>('POST', `/api/v1/nodes/${id(nodeId)}/move`, { body: { parent_id: parentId } }),
  copyNode: (nodeId: string, parentId: string) =>
    request<FsNode | AcceptedOp>('POST', `/api/v1/nodes/${id(nodeId)}/copy`, { body: { parent_id: parentId } }),
  deleteNode: (nodeId: string, purge = false) =>
    request<{ ok: boolean; purged: boolean }>('DELETE', `/api/v1/nodes/${id(nodeId)}`, { query: { purge } }),
  restoreNode: (nodeId: string) => request<FsNode>('POST', `/api/v1/nodes/${id(nodeId)}/restore`),

  // -- parts: merge + split + reorder
  mergeNode: (targetId: string, donorIds: string[], name?: string, parts?: FilePartRef[]) =>
    request<FsNode>('POST', `/api/v1/nodes/${id(targetId)}/merge`, { body: { donor_ids: donorIds, name, parts } }),
  listParts: (nodeId: string) => request<FilePart[]>('GET', `/api/v1/nodes/${id(nodeId)}/parts`),
  splitParts: (nodeId: string, partIndices: number[]) =>
    request<SplitPartsResult>('POST', `/api/v1/nodes/${id(nodeId)}/parts/split`, { body: { part_indices: partIndices } }),
  reorderParts: (nodeId: string, order: number[]) =>
    request<FsNode>('PUT', `/api/v1/nodes/${id(nodeId)}/parts`, { body: { order } }),

  // -- inline file content (edit)
  fileContent: async (nodeId: string): Promise<string> => {
    const resp = await fetch(downloadUrl(nodeId), { headers: { ...authHeaders() } })
    if (!resp.ok) throw new ApiError(resp.status, resp.statusText)
    return resp.text()
  },
  setContent: async (nodeId: string, data: string | Blob, force = false): Promise<FsNode> => {
    const url = new URL(`/api/v1/nodes/${id(nodeId)}/content`, window.location.origin)
    if (force) url.searchParams.set('force', 'true')
    const resp = await fetch(url, {
      method: 'PUT',
      headers: { ...authHeaders(), 'Content-Type': 'application/octet-stream' },
      body: data,
    })
    const text = await resp.text()
    const parsed = text ? JSON.parse(text) : undefined
    if (!resp.ok) throw new ApiError(resp.status, parsed?.error ?? resp.statusText)
    return parsed as FsNode
  },

  // -- ops
  getMetrics: () => request<MetricsSnapshot>('GET', '/metrics'),

  // -- upload (predisposed, not wired into the MVP UI; no progress yet)
  uploadFile: async (folderId: string, file: File): Promise<FsNode> => {
    const url = new URL(`/api/v1/folders/${id(folderId)}/files`, window.location.origin)
    url.searchParams.set('filename', file.name)
    const resp = await fetch(url, {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    })
    const text = await resp.text()
    const data = text ? JSON.parse(text) : undefined
    if (!resp.ok) throw new ApiError(resp.status, data?.error ?? resp.statusText)
    return data as FsNode
  },
}

// not a fetch — a URL the browser loads directly (Basic auth auto-attached)
export function downloadUrl(nodeId: string, filename?: string): string {
  return `/download/${id(nodeId)}${filename ? `/${encodeURIComponent(filename)}` : ''}`
}

export function isAccepted(r: FsNode | AcceptedOp): r is AcceptedOp {
  return (r as AcceptedOp).status === 'accepted'
}
