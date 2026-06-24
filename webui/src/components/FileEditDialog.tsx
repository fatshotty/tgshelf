// View / edit the body of an INLINE (DB-stored) file. Text mimes load into a
// textarea and can be saved in place; binary mimes are view-only (size + mime +
// a download link). A save that would push the file past the inline threshold is
// rejected with 409 — we then offer to "force" it onto Telegram (the file stops
// being inline). Only shown for inline files (see NodeMenu).
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { api, ApiError, downloadUrl } from '../api/client'
import type { FsNode } from '../api/types'
import { humanBytes, isTextMime } from '../lib/format'
import { Modal } from './Modal'

export function FileEditDialog({
  node,
  onSave,
  onClose,
}: {
  node: FsNode
  onSave: (data: string, force: boolean) => Promise<void>
  onClose: () => void
}) {
  const editable = isTextMime(node.mime)

  if (!editable) {
    return (
      <Modal title={`View — ${node.name}`} onClose={onClose}>
        <div className="dialog">
          <p>
            This is a binary file ({node.mime ?? 'unknown type'}, {humanBytes(node.size)}) — it
            can't be edited as text.
          </p>
          <div className="dialog-actions">
            <a className="btn" href={downloadUrl(node.id, node.name)} target="_blank" rel="noreferrer">
              Download
            </a>
            <button type="button" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
      </Modal>
    )
  }
  return <TextEditor node={node} onSave={onSave} onClose={onClose} />
}

function TextEditor({
  node,
  onSave,
  onClose,
}: {
  node: FsNode
  onSave: (data: string, force: boolean) => Promise<void>
  onClose: () => void
}) {
  const contentQ = useQuery({ queryKey: ['content', node.id], queryFn: () => api.fileContent(node.id) })
  const [value, setValue] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [askForce, setAskForce] = useState(false)

  // seed the textarea once the content has loaded
  const text = value ?? contentQ.data ?? ''

  const save = async (force: boolean) => {
    setBusy(true)
    setErr(null)
    try {
      await onSave(text, force)
      onClose()
    } catch (ex) {
      if (ex instanceof ApiError && ex.status === 409 && !force) {
        setAskForce(true) // too large to stay inline — offer to push to Telegram
      } else {
        setErr(ex instanceof Error ? ex.message : String(ex))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`Edit — ${node.name}`} onClose={onClose}>
      <div className="dialog">
        {contentQ.isLoading && <p>Loading…</p>}
        {contentQ.error && <div className="error">{String(contentQ.error)}</div>}
        {!contentQ.isLoading && !contentQ.error && (
          <textarea
            className="editor"
            autoFocus
            spellCheck={false}
            value={text}
            onChange={(e) => {
              setValue(e.target.value)
              setAskForce(false)
            }}
          />
        )}
        {err ? <div className="error">{err}</div> : null}
        {askForce ? (
          <div className="warn">
            The new content exceeds the inline limit. Saving will move this file onto Telegram
            (it stops being a DB-stored file).
          </div>
        ) : null}
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          {askForce ? (
            <button className="danger" disabled={busy} onClick={() => save(true)}>
              Save to Telegram
            </button>
          ) : (
            <button disabled={busy || contentQ.isLoading} onClick={() => save(false)}>
              Save
            </button>
          )}
        </div>
      </div>
    </Modal>
  )
}
