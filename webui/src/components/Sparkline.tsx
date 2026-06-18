// Minimal inline-SVG sparkline — no charting dependency. Auto-scales to the max
// of the series; flat/empty series render as a baseline. Width/height are CSS px.
export function Sparkline({
  values,
  width = 480,
  height = 64,
}: {
  values: number[]
  width?: number
  height?: number
}) {
  if (values.length < 2) {
    return <svg className="sparkline" width={width} height={height} aria-hidden />
  }
  const max = Math.max(...values, 1) // avoid /0; 1 keeps a flat line on the floor
  const stepX = width / (values.length - 1)
  const pts = values
    .map((v, i) => {
      const x = i * stepX
      const y = height - (v / max) * height
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  return (
    <svg className="sparkline" width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="2" />
    </svg>
  )
}
