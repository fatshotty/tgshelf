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

// Whether a mime is text we can safely edit as UTF-8 in a textarea. Covers
// text/* plus the common structured-text application/* types; everything else
// (images, video, octet-stream, …) is treated as binary → view only.
const TEXT_APPLICATION = /^application\/(json|xml|x-yaml|yaml|javascript|x-sh|toml|x-ndjson|sql)$/

export function isTextMime(mime: string | null): boolean {
  if (!mime) return false
  const m = mime.split(';')[0].trim().toLowerCase()
  return m.startsWith('text/') || m.endsWith('+json') || m.endsWith('+xml') || TEXT_APPLICATION.test(m)
}
