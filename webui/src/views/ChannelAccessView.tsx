import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../api/client'
import type { AccessMode } from '../api/types'

export default function ChannelAccessView() {
  const client = useQueryClient()
  const grid = useQuery({ queryKey: ['channel-access'], queryFn: api.getChannelAccess })
  const save = useMutation({
    mutationFn: ({ channel, account, mode }: { channel: number; account: string; mode: AccessMode | null }) =>
      api.setChannelAccess(channel, account, mode),
    onSuccess: (data) => client.setQueryData(['channel-access'], data),
  })
  return <section>
    <div className="toolbar">
      <h1>Channel access</h1>
      <button className="btn" disabled={grid.isFetching || save.isPending} onClick={() => grid.refetch()}>Refresh</button>
    </div>
    <p>Accounts have full control by default. Bots are read-only workers. Root access is fixed and cannot be changed.</p>
    <p>Changes affect new files and streams immediately after saving. Work already in progress keeps its permissions.</p>
    {grid.isPending && <p>Loading permissions…</p>}
    {grid.error && <p role="alert">{String(grid.error)}</p>}
    {save.error && <p role="alert">Save failed: {String(save.error)}</p>}
    <p role="status" aria-live="polite">{save.isPending ? 'Saving…' : save.isSuccess ? 'Saved.' : ''}</p>
    {grid.data && <div className="channel-access-scroll">
      {grid.data.folders.length === 0 ? <p>No folders have an explicit channel mapping.</p> : <table className="channel-access-table">
        <thead><tr><th>Folder / channel</th>{grid.data.accounts.map(a => <th key={a.name}>{a.name}<br /><small>{a.kind}</small></th>)}</tr></thead>
        <tbody>{grid.data.folders.map(folder => <tr key={folder.id}>
          <th scope="row">{folder.path}<br /><small>{folder.channel_id}</small></th>
          {grid.data!.accounts.map(account => {
            const cell = grid.data!.permissions.find(c => c.channel_id === folder.channel_id && c.account_name === account.name)
            if (!cell) return <td key={account.name}>Unavailable</td>
            return <td key={account.name}><select
              aria-label={`${account.name}: ${folder.path}`}
              disabled={cell.is_locked || save.isPending}
              value={cell.is_default ? 'default' : cell.mode}
              onChange={e => save.mutate({ channel: folder.channel_id, account: account.name, mode: e.target.value === 'default' ? null : e.target.value as AccessMode })}
            >
              <option value="default">Default ({account.kind === 'bot' ? 'Read' : 'Full control'})</option>
              <option value="none">None</option><option value="read">Read</option>
              {account.kind === 'user' && <option value="write">Full control</option>}
            </select>{cell.is_locked && <small> Fixed root channel</small>}</td>
          })}
        </tr>)}</tbody>
      </table>}
    </div>}
  </section>
}
