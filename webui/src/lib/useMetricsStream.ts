// Live metrics over SSE. Same-origin EventSource → Basic auth is attached by the
// browser automatically (see auth/credentials.ts rationale). EventSource
// reconnects on its own; we only surface a status. Derivation/sparkline logic
// lives in throughput.ts so this hook stays a thin I/O seam.
import { useEffect, useState } from 'react'

import type { MetricsSnapshot } from '../api/types'

export type StreamStatus = 'connecting' | 'live' | 'disconnected'

export function useMetricsStream(): { snapshot: MetricsSnapshot | null; status: StreamStatus } {
  const [snapshot, setSnapshot] = useState<MetricsSnapshot | null>(null)
  const [status, setStatus] = useState<StreamStatus>('connecting')

  useEffect(() => {
    const es = new EventSource('/api/v1/metrics/stream')
    es.onmessage = (ev) => {
      try {
        setSnapshot(JSON.parse(ev.data) as MetricsSnapshot)
        setStatus('live')
      } catch {
        // ignore a malformed tick — keep the last good snapshot
      }
    }
    es.onerror = () => {
      // EventSource retries automatically; just reflect the gap in the UI.
      setStatus('disconnected')
    }
    return () => es.close()
  }, [])

  return { snapshot, status }
}
