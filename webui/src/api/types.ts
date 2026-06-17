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
  ctime: string | null
  mtime: string | null
  info: Record<string, unknown>
}

// GET /api/v1/nodes/{id}/parts
export interface FilePart {
  idx: number
  size: number
  original_filename: string | null
}

// move/copy on a folder answers 202 with this instead of a node
export interface AcceptedOp {
  status: 'accepted'
  operation: 'move' | 'copy'
  node_id: string
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
