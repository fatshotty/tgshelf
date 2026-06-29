# rclone Integration Design

## Goal

Mount tgshelf as a read-write cloud drive through rclone while keeping directory
cache refresh responsive.

## Data Plane

WebDAV is served at `/dav` when `rclone.webdav_enabled` is true. It translates
WebDAV methods to FileSystem operations:

- `PROPFIND`: list/stat;
- `GET`/`HEAD`: stream node content;
- `PUT`: write whole file;
- `MKCOL`: create folder;
- `DELETE`: soft delete;
- `MOVE`: move/rename;
- `COPY`: copy;
- `OPTIONS`: advertise DAV support.

## Control Plane

rclone clients self-register their rc endpoint through request headers.

The server stores authorized endpoints in an in-memory registry with TTL. When
PostgreSQL emits changes, the bridge calls `vfs/forget` on registered rc
endpoints so rclone invalidates directory cache entries immediately.

## Security

Self-registration is disabled unless explicitly enabled. rc URLs are checked to
reduce SSRF risk: the request source address is allowed, and optional CIDR
allowlists can be configured.

## Verification

Unit tests cover rc URL parsing, registry behavior, and authorization. Manual e2e
checks require a real rclone mount with rc enabled.
