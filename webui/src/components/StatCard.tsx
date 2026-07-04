// One metric: a big value, a label, an optional sub-line (e.g. soft-limit).
export function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="statcard">
      <div className="statval">{value}</div>
      <div className="statlabel">{label}</div>
      {sub ? <div className="statsub">{sub}</div> : null}
    </div>
  )
}
