// BrowserRouter runs under /webui. Browse URLs are /webui/<fs-path>, each
// segment URL-encoded (names may contain spaces and * _ { } [ ] but never '/').
// These helpers convert between the router path and the filesystem API path.

export function pathSegments(pathname: string): string[] {
  const raw = pathname.replace(/^\/webui\/?/, '').replace(/^\//, '')
  return raw ? raw.split('/').filter(Boolean).map(decodeURIComponent) : []
}

export function fsPath(segments: string[]): string {
  return '/' + segments.join('/') // '/' = root (the API treats empty segments as root)
}

export function browseUrl(segments: string[]): string {
  return segments.length ? '/' + segments.map(encodeURIComponent).join('/') : '/'
}
