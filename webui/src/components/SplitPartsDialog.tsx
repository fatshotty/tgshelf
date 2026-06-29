import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { api } from '../api/client'
import type { FsNode } from '../api/types'
import { humanBytes } from '../lib/format'
import { Modal } from './Modal'

export function SplitPartsDialog({
  node,
  onClose,
  onSplit,
}: {
  node: FsNode
  onClose: () => void
  onSplit: (partIndices: number[]) => Promise<void>
}) {
  const partsQ = useQuery({ queryKey: ['parts', node.id], queryFn: () => api.listParts(node.id) })
  const parts = partsQ.data ?? []
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const extractsEveryPart = selected.size > 0 && selected.size === parts.length

  const submit = async () => {
    setBusy(true)
    setErr(null)
    try {
      await onSplit([...selected].sort((a, b) => a - b))
      onClose()
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`Split - ${node.name}`} onClose={onClose}>
      <div className="dialog parts-dialog">
        <section className="parts-section">
          <div className="section-head">
            <span>Parts to extract</span>
            <span className="muted">{selected.size} selected</span>
          </div>
          {partsQ.isLoading ? <div className="info compact">Loading...</div> : null}
          {partsQ.error ? <div className="error compact">{String(partsQ.error)}</div> : null}
          {parts.length === 0 && !partsQ.isLoading ? <div className="empty compact">No parts</div> : null}
          {parts.length > 0 ? (
            <div className="donor-list">
              {parts.map((part) => (
                <label className="donor-row split-row" key={part.idx}>
                  <input
                    type="checkbox"
                    checked={selected.has(part.idx)}
                    disabled={busy}
                    onChange={(e) =>
                      setSelected((prev) => {
                        const next = new Set(prev)
                        if (e.target.checked) next.add(part.idx)
                        else next.delete(part.idx)
                        return next
                      })
                    }
                  />
                  <span className="part-index">#{part.idx}</span>
                  <span className="part-name">{part.original_filename ?? `part ${part.idx}`}</span>
                  <span className="part-size">{humanBytes(part.size)}</span>
                </label>
              ))}
            </div>
          ) : null}
        </section>

        {extractsEveryPart ? <div className="error compact">Leave at least one part in the source file</div> : null}
        {err ? <div className="error compact">{err}</div> : null}
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="button" disabled={busy || selected.size === 0 || extractsEveryPart} onClick={submit}>
            Extract selected
          </button>
        </div>
      </div>
    </Modal>
  )
}
