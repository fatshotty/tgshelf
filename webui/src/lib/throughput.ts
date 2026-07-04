// Pure helpers for the live metrics dashboard: derive a bytes/sec rate from the
// cumulative `bytes_total` counter between two SSE ticks, and keep a fixed-size
// window of samples for the sparkline. No React, no I/O — unit-testable as-is.

export interface Sample {
  bytes: number // stream.bytes_total at this tick (cumulative counter)
  t: number // epoch ms when the tick was received
}

// bytes/sec between two samples. dt is measured (not assumed 1s). Guards:
// non-positive dt → 0; counter going backwards (server restart) → 0, not a
// negative spike.
export function deriveRate(prev: Sample, cur: Sample): number {
  const dtMs = cur.t - prev.t
  if (dtMs <= 0) return 0
  if (cur.bytes < prev.bytes) return 0
  return ((cur.bytes - prev.bytes) * 1000) / dtMs
}

// Fixed-size FIFO window. push() drops the oldest once full; values() returns a
// copy oldest→newest for plotting.
export class RingBuffer {
  private buf: number[] = []
  constructor(private readonly capacity: number) {}
  push(v: number): void {
    this.buf.push(v)
    if (this.buf.length > this.capacity) this.buf.shift()
  }
  values(): number[] {
    return [...this.buf]
  }
}
