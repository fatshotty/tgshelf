// Fase 5 — live Stats dashboard. SSE-fed (useMetricsStream): summary cards, a
// derived-throughput sparkline (bytes/sec from the bytes_total counter), and a
// per-member tile grid for the client and bot pools.
import { useEffect, useRef, useState } from 'react'

import type { PoolStatus } from '../api/types'
import { PoolTile } from '../components/PoolTile'
import { Sparkline } from '../components/Sparkline'
import { StatCard } from '../components/StatCard'
import { humanBytes, humanRate } from '../lib/format'
import { deriveRate, RingBuffer, type Sample } from '../lib/throughput'
import { useMetricsStream } from '../lib/useMetricsStream'

const WINDOW = 60 // ~1 min of samples at ~1 tick/s

export default function MetricsView() {
  const { snapshot, status } = useMetricsStream()
  const ring = useRef(new RingBuffer(WINDOW))
  const prev = useRef<Sample | null>(null)
  const [rate, setRate] = useState(0)
  const [series, setSeries] = useState<number[]>([])

  const totalBytes = snapshot?.stream.bytes_total
  useEffect(() => {
    if (totalBytes === undefined) return
    const cur: Sample = { bytes: totalBytes, t: Date.now() }
    if (prev.current) {
      const r = deriveRate(prev.current, cur)
      setRate(r)
      ring.current.push(r)
      setSeries(ring.current.values())
    }
    prev.current = cur
  }, [totalBytes])

  if (!snapshot) {
    return (
      <div className="panel">{status === 'disconnected' ? 'Disconnected — retrying…' : 'Connecting…'}</div>
    )
  }

  const s = snapshot.stream
  const soft = s.memory_soft_limit ?? 0
  return (
    <div className={`metrics${status === 'disconnected' ? ' stale' : ''}`}>
      {status === 'disconnected' && <div className="banner warn">Reconnecting…</div>}

      <div className="cards">
        <StatCard label="Active streams" value={String(s.active_streams ?? 0)} />
        <StatCard label="Throughput" value={humanRate(rate)} />
        <StatCard
          label="Buffered"
          value={humanBytes(s.buffered_bytes ?? 0)}
          sub={soft ? `soft limit ${humanBytes(soft)}` : undefined}
        />
        <StatCard label="Streams total" value={String(s.streams_total ?? 0)} />
        <StatCard label="Bytes total" value={humanBytes(s.bytes_total ?? 0)} />
        <StatCard label="Degraded" value={String(s.degraded_total ?? 0)} />
      </div>

      <div className="sparkwrap">
        <div className="sparkhead">Throughput · {humanRate(rate)}</div>
        <Sparkline values={series} />
      </div>

      <PoolSection title="Clients" pool={snapshot.pools.clients} />
      <PoolSection title="Bots" pool={snapshot.pools.bots} />
    </div>
  )
}

function PoolSection({ title, pool }: { title: string; pool: PoolStatus }) {
  return (
    <section className="poolsec">
      <header className="poolhead">
        <span className="pooltitle">{title}</span>
        <span className="poolstat">
          {pool.available}/{pool.total} available · {pool.in_flight} in-flight
        </span>
      </header>
      {pool.members.length === 0 ? (
        <div className="empty">no members</div>
      ) : (
        <div className="poolgrid">
          {pool.members.map((m) => (
            <PoolTile key={m.name} member={m} />
          ))}
        </div>
      )}
    </section>
  )
}
