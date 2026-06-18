// Small dialogs built on Modal. PromptDialog drives rename + set-channel (single
// text/number field); it keeps the modal open and shows the error inline when the
// submit promise rejects (e.g. 409 duplicate name). ConfirmDialog drives delete
// (soft) and purge (danger: a checkbox must be ticked to enable the button).
import { type FormEvent, useState } from 'react'

import { Modal } from './Modal'

export function PromptDialog({
  title,
  label,
  initial,
  placeholder,
  onClose,
  onSubmit,
}: {
  title: string
  label: string
  initial?: string
  placeholder?: string
  onClose: () => void
  onSubmit: (value: string) => Promise<void>
}) {
  const [value, setValue] = useState(initial ?? '')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setErr(null)
    try {
      await onSubmit(value)
      onClose()
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={title} onClose={onClose}>
      <form className="dialog" onSubmit={submit}>
        <label className="dialog-label">{label}</label>
        <input autoFocus value={value} placeholder={placeholder} onChange={(e) => setValue(e.target.value)} />
        {err ? <div className="error">{err}</div> : null}
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={busy}>
            OK
          </button>
        </div>
      </form>
    </Modal>
  )
}

export function ConfirmDialog({
  title,
  message,
  danger,
  confirmLabel,
  onClose,
  onConfirm,
}: {
  title: string
  message: string
  danger?: boolean
  confirmLabel?: string
  onClose: () => void
  onConfirm: () => Promise<void>
}) {
  const [ack, setAck] = useState(!danger) // non-danger: pre-acknowledged
  const [busy, setBusy] = useState(false)

  const go = async () => {
    setBusy(true)
    try {
      await onConfirm()
    } finally {
      setBusy(false)
      onClose()
    }
  }

  return (
    <Modal title={title} onClose={onClose}>
      <div className="dialog">
        <p>{message}</p>
        {danger ? (
          <label className="ack">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            I understand this is permanent
          </label>
        ) : null}
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button className={danger ? 'danger' : ''} disabled={!ack || busy} onClick={go}>
            {confirmLabel ?? 'Confirm'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
