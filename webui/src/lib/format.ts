const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']

export function humanBytes(n: number): string {
  let v = n
  for (const u of UNITS) {
    if (v < 1024 || u === 'PB') return u === 'B' ? `${v} B` : `${v.toFixed(1)} ${u}`
    v /= 1024
  }
  return `${v.toFixed(1)} PB`
}

export function humanRate(bytesPerSec: number): string {
  return `${humanBytes(bytesPerSec)}/s`
}
