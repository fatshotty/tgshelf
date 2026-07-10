// Composite file editor: filename + info.notes + inline text content through
// PUT /api/v1/nodes/{id}. Binary files remain metadata-only here.
import { useQuery } from '@tanstack/react-query'
import { type FormEvent, useState } from 'react'

import { api, downloadUrl, type NodeUpdate } from '../api/client'
import type { FsNode } from '../api/types'
import { humanBytes, isTextFile } from '../lib/format'
import { Modal } from './Modal'

const NOTES_MAX = 200

export function FileEditDialog({
  node,
  onSave,
  onClose,
}: {
  node: FsNode
  onSave: (body: NodeUpdate) => Promise<void>
  onClose: () => void
}) {
  const isText = isTextFile(node.name, node.mime)
  const contentEditable = isText && node.inline
  const notes = typeof node.info?.notes === 'string' ? node.info.notes : ''
  const contentQ = useQuery({
    queryKey: ['content', node.id],
    queryFn: () => api.fileContent(node.id),
    enabled: contentEditable,
  })
  const [name, setName] = useState(node.name)
  const [notesValue, setNotesValue] = useState(notes)
  const [contentValue, setContentValue] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const text = contentValue ?? contentQ.data ?? ''
  const notesTooLong = notesValue.length > NOTES_MAX

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (notesTooLong) return
    setBusy(true)
    setErr(null)
    try {
      const body: NodeUpdate = {
        name,
        info: { notes: notesValue },
      }
      if (contentEditable) body.content = text
      await onSave(body)
      onClose()
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`Edit — ${node.name}`} onClose={onClose} className="file-edit-modal">
      <form className="dialog" onSubmit={submit}>
        <label className="dialog-label">Filename</label>
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} />

        <label className="dialog-label">Notes</label>
        <textarea
          className="notes-editor"
          value={notesValue}
          maxLength={NOTES_MAX + 1}
          onChange={(e) => setNotesValue(e.target.value)}
        />
        <div className={notesTooLong ? 'error compact' : 'muted compact'}>
          {notesValue.length}/{NOTES_MAX}
        </div>

        <label className="dialog-label">Content</label>
        {!isText ? (
          <div className="readonly-box">
            Binary file ({node.mime ?? 'unknown type'}, {humanBytes(node.size)})
          </div>
        ) : !node.inline ? (
          <div className="readonly-box">
            Text file stored on Telegram ({humanBytes(node.size)})
          </div>
        ) : contentQ.isLoading ? (
          <div className="info compact">Loading...</div>
        ) : contentQ.error ? (
          <div className="error">{String(contentQ.error)}</div>
        ) : (
          <textarea
            className="editor"
            spellCheck={false}
            value={text}
            onChange={(e) => setContentValue(e.target.value)}
          />
        )}

        {err ? <div className="error">{err}</div> : null}
        <div className="dialog-actions">
          <a className="btn" href={downloadUrl(node.id, node.name)} target="_blank" rel="noreferrer">
            Download
          </a>
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={busy || notesTooLong || contentQ.isLoading}>
            Save
          </button>
        </div>
      </form>
    </Modal>
  )
}
