// Mirrors src/tgshelf/http/schemas.py::node_to_dict (note: no parent_id — the UI
// navigates by path, not by parent links).
export interface FsNode {
  id: string
  name: string
  is_folder: boolean
  mime: string | null
  channel_id: number | null
  state: string // ACTIVE | TEMP | DELETED
  size: number
  inline: boolean // stored in the DB (editable in place) vs Telegram-backed
  ctime: string | null
  mtime: string | null
  info: Record<string, unknown>
}

export interface SearchResult {
  node: FsNode
  path: string
  parent_path: string
}

// GET /api/v1/nodes/{id}/size — a file's own size, or a folder's recursive total
export interface NodeSize {
  id: string
  is_folder: boolean
  size: number
}

// GET /api/v1/nodes/{id}/parts
export interface FilePart {
  idx: number
  size: number
  original_filename: string | null
}

export interface FilePartRef {
  file_id: string
  idx: number
}

export interface SplitPartsResult {
  source: FsNode
  extracted: FsNode[]
}

export interface StrmResult {
  destination: string
  created: number
  updated: number
  skipped: number
  removed: number
  inline: number
}

// move/copy on a folder answers 202 with this instead of a node
export interface AcceptedOp {
  status: 'accepted'
  operation: 'move' | 'copy'
  node_id: string
}

export type OperationKind = 'move' | 'delete'
export type OperationJobState = 'queued' | 'running' | 'completed' | 'failed' | 'interrupted'
export type OperationJobItemState = 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped'

export interface OperationJobSummary {
  id: string
  operation: OperationKind
  state: OperationJobState
  parent_id: string | null
  total: number
  succeeded: number
  failed: number
  skipped: number
  error: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface OperationJobItem {
  position: number
  node_id: string
  source_name: string | null
  source_path: string | null
  state: OperationJobItemState
  error: string | null
  started_at: string | null
  finished_at: string | null
}

export interface OperationJobDetail extends OperationJobSummary {
  items: OperationJobItem[]
}

export interface OperationJobPage {
  items: OperationJobSummary[]
  next_offset: number | null
}

export interface OperationJobAccepted {
  id: string
  state: 'queued'
  status_url: string
}

// GET /metrics (JSON) and the SSE stream payloads
export interface MetricsSnapshot {
  pools: { clients: PoolStatus; bots: PoolStatus }
  stream: StreamMetrics
}

export interface PoolStatus {
  total: number
  available: number
  in_flight: number
  members: PoolMember[]
}

export interface PoolMember {
  name: string
  is_premium?: boolean
  capacity?: number
  in_flight: number
  load: number
  quarantined: boolean
  cooldown_remaining: number
  consecutive_errors?: number
  ineligible_channels: number[]
  available: boolean
}

export interface StreamMetrics {
  configured_k?: number
  memory_soft_limit?: number
  active_streams?: number
  buffered_bytes?: number
  streams_total?: number
  bytes_total?: number
  degraded_total?: number
}
