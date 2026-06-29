import { useQueries } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'

import { api } from '../api/client'
import type { FilePart, FilePartRef, FsNode } from '../api/types'
import { humanBytes } from '../lib/format'
import { Modal } from './Modal'

interface MergePart {
  key: string
  fileId: string
  fileName: string
  part: FilePart
}

function swap(values: string[], a: number, b: number): string[] {
  const next = [...values]
  const tmp = next[a]
  next[a] = next[b]
  next[b] = tmp
  return next
}

function baseName(name: string): string {
  return name.replace(/\.\d{3,}$/, '')
}

function suggestedName(files: FsNode[], parts: MergePart[]): string {
  const partNames = parts.map((p) => p.part.original_filename).filter(Boolean) as string[]
  const bases = (partNames.length > 0 ? partNames : files.map((f) => f.name)).map(baseName)
  const first = bases[0] || files[0]?.name || 'merged-file'
  return bases.every((name) => name.toLowerCase() === first.toLowerCase()) ? first : baseName(files[0]?.name || first)
}

export function MergePartsDialog({
  files,
  onClose,
  onMerge,
}: {
  files: FsNode[]
  onClose: () => void
  onMerge: (targetId: string, donorIds: string[], name: string, parts: FilePartRef[]) => Promise<void>
}) {
  const queries = useQueries({
    queries: files.map((file) => ({
      queryKey: ['parts', file.id],
      queryFn: () => api.listParts(file.id),
    })),
  })
  const [name, setName] = useState('')
  const [order, setOrder] = useState<string[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const parts = useMemo(
    () =>
      files.flatMap((file, filePos) =>
        (queries[filePos].data ?? []).map((part) => ({
          key: `${file.id}:${part.idx}`,
          fileId: file.id,
          fileName: file.name,
          part,
        })),
      ),
    [files, queries],
  )
  const signature = parts.map((part) => part.key).join('|')

  useEffect(() => {
    if (parts.length === 0) return
    setOrder(parts.map((part) => part.key))
    setName((current) => current || suggestedName(files, parts))
  }, [signature])

  const ordered = order.map((key) => parts.find((part) => part.key === key)).filter(Boolean) as MergePart[]
  const loading = queries.some((query) => query.isLoading)
  const error = queries.find((query) => query.error)?.error

  const submit = async () => {
    setBusy(true)
    setErr(null)
    try {
      const target = files[0]
      await onMerge(
        target.id,
        files.slice(1).map((file) => file.id),
        name.trim(),
        ordered.map((row) => ({ file_id: row.fileId, idx: row.part.idx })),
      )
      onClose()
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="Merge parts" onClose={onClose}>
      <div className="dialog parts-dialog">
        <label className="dialog-label">File name</label>
        <input autoFocus value={name} onChange={(e) => setName(e.target.value)} />

        <section className="parts-section">
          <div className="section-head">
            <span>Parts</span>
            <span className="muted">{ordered.length} selected</span>
          </div>
          {loading ? <div className="info compact">Loading...</div> : null}
          {error ? <div className="error compact">{String(error)}</div> : null}
          {ordered.length > 0 ? (
            <div className="parts-list">
              {ordered.map((row, pos) => (
                <div className="part-row merge-row" key={row.key}>
                  <span className="part-index">#{row.part.idx}</span>
                  <span className="part-name">{row.part.original_filename ?? row.fileName}</span>
                  <span className="part-source">{row.fileName}</span>
                  <span className="part-size">{humanBytes(row.part.size)}</span>
                  <button type="button" disabled={pos === 0 || busy} onClick={() => setOrder((v) => swap(v, pos, pos - 1))}>
                    Up
                  </button>
                  <button type="button" disabled={pos === ordered.length - 1 || busy} onClick={() => setOrder((v) => swap(v, pos, pos + 1))}>
                    Down
                  </button>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        {err ? <div className="error compact">{err}</div> : null}
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="button" disabled={busy || loading || ordered.length === 0 || !name.trim()} onClick={submit}>
            Merge
          </button>
        </div>
      </div>
    </Modal>
  )
}
