// Tree-management actions as thin async wrappers over api.*, each invalidating the
// affected react-query caches. Folder move/copy answers 202 (background) — we toast
// and re-invalidate after a short delay since there is no completion signal. Errors
// reject (ApiError) so callers can show them inline (dialogs) or via toast.
import { useQueryClient } from '@tanstack/react-query'

import { api, isAccepted } from '../api/client'
import type { AcceptedOp, FilePartRef, FsNode, StrmResult } from '../api/types'
import { useToast } from '../components/Toast'

function strmSummary(stats: StrmResult): string {
  return `${stats.created} created, ${stats.updated} updated, ${stats.skipped} unchanged, ${stats.removed} removed`
}

export function useTreeActions(folderId: string | undefined, path: string) {
  const qc = useQueryClient()
  const toast = useToast()

  const invalidate = () => {
    if (folderId) qc.invalidateQueries({ queryKey: ['children', folderId] })
    qc.invalidateQueries({ queryKey: ['resolve', path] })
  }

  const afterMoveCopy = (r: FsNode | AcceptedOp, verb: string) => {
    if (isAccepted(r)) {
      toast(`${verb} started in background (may take a while)`, 'info')
      setTimeout(invalidate, 3000)
    } else {
      invalidate()
    }
  }

  return {
    createFolder: (name: string) => api.createFolder(folderId!, name).then(invalidate),
    rename: (id: string, name: string) => api.rename(id, name).then(invalidate),
    setChannel: (id: string, channelId: number | null) =>
      api.setFolderChannel(id, channelId).then(invalidate),
    remove: (id: string, purge: boolean) => api.deleteNode(id, purge).then(invalidate),
    restore: (id: string) => api.restoreNode(id).then(invalidate),
    move: (id: string, destId: string) => api.moveNode(id, destId).then((r) => afterMoveCopy(r, 'Move')),
    copy: (id: string, destId: string) => api.copyNode(id, destId).then((r) => afterMoveCopy(r, 'Copy')),
    setContent: (id: string, data: string | Blob, force = false) =>
      api.setContent(id, data, force).then(invalidate),
    mergeParts: (id: string, donorIds: string[], name?: string, parts?: FilePartRef[]) =>
      api.mergeNode(id, donorIds, name, parts).then(() => {
        invalidate()
        qc.invalidateQueries({ queryKey: ['parts', id] })
      }),
    splitParts: (id: string, partIndices: number[]) =>
      api.splitParts(id, partIndices).then(() => {
        invalidate()
        qc.invalidateQueries({ queryKey: ['parts', id] })
      }),
    reorderParts: (id: string, order: number[]) =>
      api.reorderParts(id, order).then(() => {
        invalidate()
        qc.invalidateQueries({ queryKey: ['parts', id] })
      }),
    generateStrm: (id: string) =>
      api.generateStrm(id).then((r) => {
        toast(`STRM generated: ${strmSummary(r)}`, 'info')
        return r
      }),
    deleteStrm: (id: string) =>
      api.deleteStrm(id).then((r) => {
        toast(`STRM deleted: ${strmSummary(r)}`, 'info')
        return r
      }),
  }
}
