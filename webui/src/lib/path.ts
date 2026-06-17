// The browse URL is /b/<fs-path>, each segment URL-encoded (names may contain
// spaces and * _ { } [ ] but never '/'). These helpers convert between the URL
// and the filesystem path used by the API.

export function pathSegments(pathname: string): string[] {
  const raw = pathname.replace(/^\/b\/?/, '')
  return raw ? raw.split('/').filter(Boolean).map(decodeURIComponent) : []
}

export function fsPath(segments: string[]): string {
  return '/' + segments.join('/') // '/' = root (the API treats empty segments as root)
}

export function browseUrl(segments: string[]): string {
  return segments.length ? '/b/' + segments.map(encodeURIComponent).join('/') : '/b'
}
