"""Streaming download endpoint — the single byte-serving route.

`GET /download/{file_id}` and `GET /download/{file_id}/{tail:.*}` map to the
same handler: the node is resolved SOLELY by `file_id` (the first path segment);
the rest of the path and the entire query string are accepted and IGNORED
(decisione utente — the .strm template is free to decorate the URL for players).

Serves a single Range (206 + Content-Range), 416 with `Content-Range: bytes
*/size` on an unsatisfiable one, an ETag with If-None-Match -> 304, and HEAD
(headers only). Bytes come from `fs.open_read`, written straight to the socket;
the generator is closed in a `finally` so the streamer releases its bots.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from aiohttp import web

from tgshelf.http.api import open_fs

log = logging.getLogger("tgshelf.http.download")

# A streaming client closing mid-transfer surfaces here as one of these while
# writing to the socket. It is not a server fault: swallow it where it happens
# so nothing escapes to be logged with a stacktrace (by us OR by aiohttp.server).
_CLIENT_GONE = (ConnectionResetError, ConnectionError, asyncio.CancelledError)


class _BadRange(Exception):
    """Range header present but not satisfiable for this file's size."""


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single HTTP byte range against `size`.

    Returns (start, end) inclusive for a partial request, or None for a full
    request (no/!bytes header). Raises _BadRange when a range is present but
    cannot be satisfied (multi-range is treated as full, RFC-permitted).
    """
    if not header:
        return None
    header = header.strip()
    if not header.startswith("bytes=") or "," in header:
        return None  # not a byte range / multi-range -> serve the whole file
    spec = header[len("bytes=") :].strip()
    lo, sep, hi = spec.partition("-")
    if not sep:
        return None
    try:
        if not lo:  # suffix: bytes=-N  -> last N bytes
            n = int(hi)
            if n <= 0:
                raise _BadRange(header)
            start = max(0, size - n)
            return start, size - 1
        start = int(lo)
        end = int(hi) if hi else size - 1
    except ValueError:
        raise _BadRange(header)
    if start < 0 or start >= size or start > end:
        raise _BadRange(header)
    return start, min(end, size - 1)


def _content_disposition(name: str) -> str:
    # inline so players render in place; ASCII fallback + RFC 5987 filename*.
    ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


async def download(request: web.Request) -> web.StreamResponse:
    file_id = request.match_info["file_id"]
    async with open_fs(request) as fs:
        node = await fs.get(file_id)
        if node is None or node.is_folder or node.state != "ACTIVE":
            return web.json_response(
                {"error": f"file {file_id} not found"}, status=404
            )

        size = node.size
        etag = f'"{node.id}-{size}"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})

        try:
            rng = parse_range(request.headers.get("Range"), size)
        except _BadRange:
            return web.Response(
                status=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )

        if rng is None:
            start, end, status = 0, max(size - 1, 0), 200
        else:
            start, end = rng
            status = 206

        resp = web.StreamResponse(status=status)
        resp.headers["Content-Type"] = node.mime or "application/octet-stream"
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["Content-Disposition"] = _content_disposition(node.name)
        resp.headers["ETag"] = etag
        length = 0 if size == 0 else end - start + 1
        resp.content_length = length
        if status == 206:
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"

        await resp.prepare(request)
        if request.method == "HEAD" or size == 0:
            await resp.write_eof()
            return resp

        log.info(
            "download %s '%s' bytes %d-%d/%d", node.id, node.name, start, end, size
        )
        stream = fs.open_read(file_id, start, end)
        try:
            async for chunk in stream:
                await resp.write(chunk)
            await resp.write_eof()
        except _CLIENT_GONE as exc:
            # client went away mid-stream: quiet log, return the started response
            # so nothing propagates (CancelledError is re-raised to honour
            # cooperative cancellation).
            log.debug("download %s: client disconnected (%s)",
                      node.id, exc.__class__.__name__)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            await stream.aclose()  # release the streamer's bots on disconnect too
        return resp


def register_download_routes(app: web.Application) -> None:
    app.router.add_get("/download/{file_id}", download)
    app.router.add_get("/download/{file_id}/{tail:.*}", download)
