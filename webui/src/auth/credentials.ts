// Auth seam. MVP: the whole origin is behind HTTP Basic, so the browser prompts
// once and auto-attaches the credentials to every same-origin request (fetch and
// EventSource alike) — the SPA needs to add NOTHING. This is the single place a
// future token/login-form would change (return `{ Authorization: 'Bearer …' }`).
export function authHeaders(): Record<string, string> {
  return {}
}
