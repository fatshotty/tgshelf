// One pool member (a user client or a bot). Shows live load + health flags. The
// load bar width is `load` (0–1) clamped to [0,1]; the tile dims when the member
// is not currently available (quarantined or otherwise ineligible).
import type { PoolMember } from '../api/types'

export function PoolTile({ member }: { member: PoolMember }) {
  const loadPct = Math.max(0, Math.min(1, member.load)) * 100
  const cap = member.capacity ?? 0
  return (
    <div className={`pooltile${member.available ? '' : ' off'}`}>
      <div className="tilehead">
        <span className="tilename">{member.name}</span>
        {member.is_premium ? <span className="badge">premium</span> : null}
        {member.quarantined ? (
          <span className="badge warn">quarantined {Math.ceil(member.cooldown_remaining)}s</span>
        ) : null}
      </div>
      <div className="loadbar">
        <div className="loadfill" style={{ width: `${loadPct.toFixed(0)}%` }} />
      </div>
      <div className="tilemeta">
        <span>
          {member.in_flight}
          {cap ? `/${cap}` : ''} in-flight
        </span>
        {member.consecutive_errors ? <span>{member.consecutive_errors} err</span> : null}
        {member.ineligible_channels.length ? (
          <span>{member.ineligible_channels.length} ineligible</span>
        ) : null}
      </div>
    </div>
  )
}
