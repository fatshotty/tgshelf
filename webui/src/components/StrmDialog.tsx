import { useState } from 'react'

import type { FsNode } from '../api/types'
import { Modal } from './Modal'

type PendingAction = 'generate' | 'delete' | null

export function StrmDialog({
  node,
  onClose,
  onGenerate,
  onDelete,
}: {
  node: FsNode
  onClose: () => void
  onGenerate: () => Promise<unknown>
  onDelete: () => Promise<unknown>
}) {
  const [pending, setPending] = useState<PendingAction>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const run = async () => {
    if (pending == null) return
    setBusy(true)
    setErr(null)
    try {
      if (pending === 'generate') await onGenerate()
      else await onDelete()
      onClose()
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  const title = pending === 'generate' ? 'Generate STRM' : pending === 'delete' ? 'Delete STRM' : 'STRM'
  const message =
    pending === 'generate'
      ? `Generate STRM output for "${node.name}"?`
      : pending === 'delete'
        ? `Delete STRM output for "${node.name}"?`
        : null

  return (
    <Modal title={`${title} — ${node.name}`} onClose={onClose}>
      <div className="dialog">
        {message ? (
          <>
            <p>{message}</p>
            {err ? <div className="error">{err}</div> : null}
            <div className="dialog-actions">
              <button type="button" disabled={busy} onClick={() => setPending(null)}>
                No
              </button>
              <button type="button" className={pending === 'delete' ? 'danger' : ''} disabled={busy} onClick={run}>
                Yes
              </button>
            </div>
          </>
        ) : (
          <>
            {err ? <div className="error">{err}</div> : null}
            <div className="dialog-actions">
              <button type="button" onClick={onClose}>
                Close
              </button>
              <button type="button" onClick={() => setPending('delete')}>
                Delete
              </button>
              <button type="button" onClick={() => setPending('generate')}>
                Generate
              </button>
            </div>
          </>
        )}
      </div>
    </Modal>
  )
}
