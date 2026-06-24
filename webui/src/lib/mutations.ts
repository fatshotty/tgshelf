// Tree-management actions as thin async wrappers over api.*, each invalidating the
// affected react-query caches. Folder move/copy answers 202 (background) — we toast
// and re-invalidate after a short delay since there is no completion signal. Errors
// reject (ApiError) so callers can show them inline (dialogs) or via toast.
import { useQueryClient } from '@tanstack/react-query'

import { api, isAccepted } from '../api/client'
import type { AcceptedOp, FsNode } from '../api/types'
import { useToast } from '../components/Toast'

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
  }
}
