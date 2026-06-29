import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'

import { api } from '../api/client'
import type { FilePart, FsNode } from '../api/types'
import { humanBytes } from '../lib/format'
import { Modal } from './Modal'

function swap(values: number[], a: number, b: number): number[] {
  const next = [...values]
  const tmp = next[a]
  next[a] = next[b]
  next[b] = tmp
  return next
}

export function PartsDialog({
  node,
  candidates,
  onClose,
  onMerge,
  onReorder,
}: {
  node: FsNode
  candidates: FsNode[]
  onClose: () => void
  onMerge: (donorIds: string[]) => Promise<void>
  onReorder: (order: number[]) => Promise<void>
}) {
  const partsQ = useQuery({ queryKey: ['parts', node.id], queryFn: () => api.listParts(node.id) })
  const parts = partsQ.data ?? []
  const [order, setOrder] = useState<number[]>([])
  const [donors, setDonors] = useState<Set<string>>(new Set())
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (partsQ.data) setOrder(partsQ.data.map((p) => p.idx))
  }, [partsQ.data])

  const donorFiles = useMemo(
    () => candidates.filter((n) => n.state === 'ACTIVE' && !n.is_folder && !n.inline && n.id !== node.id),
    [candidates, node.id],
  )

  const orderedParts: FilePart[] = order.map((idx) => parts.find((p) => p.idx === idx)).filter(Boolean) as FilePart[]
  const changed = parts.length > 0 && order.some((idx, pos) => idx !== parts[pos]?.idx)

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    setErr(null)
    try {
      await fn()
      onClose()
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`Parts - ${node.name}`} onClose={onClose}>
      <div className="dialog parts-dialog">
        <section className="parts-section">
          <div className="section-head">
            <span>Order</span>
            <span className="muted">{humanBytes(node.size)}</span>
          </div>
          {partsQ.isLoading ? <div className="info compact">Loading…</div> : null}
          {partsQ.error ? <div className="error compact">{String(partsQ.error)}</div> : null}
          {parts.length === 0 && !partsQ.isLoading ? <div className="empty compact">No parts</div> : null}
          {orderedParts.length > 0 ? (
            <div className="parts-list">
              {orderedParts.map((part, pos) => (
                <div className="part-row" key={part.idx}>
                  <span className="part-index">#{part.idx}</span>
                  <span className="part-name">{part.original_filename ?? `part ${part.idx}`}</span>
                  <span className="part-size">{humanBytes(part.size)}</span>
                  <button type="button" disabled={pos === 0 || busy} onClick={() => setOrder((v) => swap(v, pos, pos - 1))}>
                    Up
                  </button>
                  <button
                    type="button"
                    disabled={pos === orderedParts.length - 1 || busy}
                    onClick={() => setOrder((v) => swap(v, pos, pos + 1))}
                  >
                    Down
                  </button>
                </div>
              ))}
            </div>
          ) : null}
          <div className="dialog-actions">
            <button type="button" disabled={!changed || busy} onClick={() => run(() => onReorder(order))}>
              Save order
            </button>
          </div>
        </section>

        <section className="parts-section">
          <div className="section-head">
            <span>Merge donors</span>
            <span className="muted">{donors.size} selected</span>
          </div>
          {donorFiles.length === 0 ? (
            <div className="empty compact">No eligible files in this folder</div>
          ) : (
            <div className="donor-list">
              {donorFiles.map((file) => (
                <label className="donor-row" key={file.id}>
                  <input
                    type="checkbox"
                    checked={donors.has(file.id)}
                    disabled={busy}
                    onChange={(e) =>
                      setDonors((prev) => {
                        const next = new Set(prev)
                        if (e.target.checked) next.add(file.id)
                        else next.delete(file.id)
                        return next
                      })
                    }
                  />
                  <span className="part-name">{file.name}</span>
                  <span className="part-size">{humanBytes(file.size)}</span>
                </label>
              ))}
            </div>
          )}
          {err ? <div className="error compact">{err}</div> : null}
          <div className="dialog-actions">
            <button type="button" onClick={onClose}>
              Cancel
            </button>
            <button type="button" disabled={donors.size === 0 || busy} onClick={() => run(() => onMerge([...donors]))}>
              Merge selected
            </button>
          </div>
        </section>
      </div>
    </Modal>
  )
}
