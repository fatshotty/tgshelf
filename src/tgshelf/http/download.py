"""Streaming download endpoint — the single byte-serving route.

`GET /download/{file_id}` and `GET /download/{file_id}/{tail:.*}` map to the
same handler: the node is resolved SOLELY by `file_id` (the first path segment);
the rest of the path and the entire query string are accepted and IGNORED
(user decision: the .strm template is free to decorate the URL for players).

Serves a single Range (206 + Content-Range), 416 with `Content-Range: bytes
*/size` on an unsatisfiable one, an ETag with If-None-Match -> 304, and HEAD
(headers only). Bytes come from `fs.open_read`, written straight to the socket;
the generator is closed in a `finally` so the streamer releases its bots.

The byte-serving core is `stream_node`, shared with the WebDAV GET handler so
the mount honours the exact same Range/ETag/streaming semantics.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from aiohttp import web

from tgshelf.http.api import open_fs
from tgshelf.log import CLIENT_DISCONNECT, current_request_id, new_request_id
from tgshelf.telegram.errors import ChannelUnavailable, FloodCooldown, PartMissing

log = logging.getLogger("tgshelf.http.download")

# A streaming client closing mid-transfer surfaces here as one of these while
# writing to the socket. It is not a server fault: swallow it where it happens
# so nothing escapes to be logged with a stacktrace (by us OR by aiohttp.server).
_CLIENT_GONE = CLIENT_DISCONNECT

# the streamer exhausted its failover (every bot flooding/unavailable) or the
# part is gone from Telegram: an expected backend condition, not a server bug.
_STREAM_ABORTED = (FloodCooldown, ChannelUnavailable, PartMissing)


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
    new_request_id()  # tag this request + its streamer workers in the log
    async with open_fs(request) as fs:
        node = await fs.get(file_id)
        if node is None or node.is_folder or node.state != "ACTIVE":
            return web.json_response(
                {"error": f"file {file_id} not found"}, status=404
            )
        return await stream_node(request, fs, node)


async def stream_node(request: web.Request, fs, node) -> web.StreamResponse:
    """Serve `node`'s bytes from `fs.open_read` (Range/206/416/ETag/304/HEAD).

    `node` is an already-resolved ACTIVE file. Shared by the `/download` route
    (resolves by file_id) and the WebDAV GET handler (resolves by path) so both
    honour the exact same byte semantics.
    """
    size = node.size
    # mtime folded into the ETag: a parts-reorder keeps the size but changes
    # the byte layout, so size alone wouldn't invalidate caches/players.
    mtime_us = int(node.mtime.timestamp() * 1_000_000)
    etag = f'"{node.id}-{size}-{mtime_us}"'
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

    log_download_start(
        node=node,
        method=request.method,
        status=status,
        start=start,
        end=end,
        size=size,
        length=length,
    )
    stream = fs.open_read(node.id, start, end)
    sent = 0
    outcome = "completed"
    try:
        async for chunk in stream:
            await resp.write(chunk)
            sent += len(chunk)
        await resp.write_eof()
    except _CLIENT_GONE as exc:
        # client went away mid-stream: quiet log, return the started response
        # so nothing propagates (CancelledError is re-raised to honour
        # cooperative cancellation).
        outcome = f"client disconnected ({exc.__class__.__name__})"
        log.debug("download %s: client disconnected (%s)",
                  node.id, exc.__class__.__name__)
        if isinstance(exc, asyncio.CancelledError):
            raise
    except _STREAM_ABORTED as exc:
        # the streamer gave up (all bots flooding/unavailable, part gone): the
        # response already started, so we can't change the status — log it
        # cleanly (no stacktrace) and end the (truncated) response. Pre-stream
        # occurrences map to a proper status via the error middleware.
        outcome = f"aborted ({exc.__class__.__name__})"
        log.warning("[download] %s stream aborted (%s): %s",
                    node.id, exc.__class__.__name__, exc)
    finally:
        await stream.aclose()  # release the streamer's bots on disconnect too
        # one terminal line per request: how it ended + bytes actually served
        # (runs on every exit incl. cancellation, before it re-propagates).
        log.info(
            "[download] done req=%s file=%s outcome=%s sent=%d/%d",
            current_request_id(),
            node.id,
            outcome,
            sent,
            length,
        )
    return resp


def log_download_start(
    *,
    node,
    method: str,
    status: int,
    start: int,
    end: int,
    size: int,
    length: int,
) -> None:
    log.info(
        "[download] start req=%s method=%s file=%s name=%r status=%d range=%d-%d/%d length=%d",
        current_request_id(),
        method,
        node.id,
        node.name,
        status,
        start,
        end,
        size,
        length,
    )


def register_download_routes(app: web.Application) -> None:
    app.router.add_get("/download/{file_id}", download)
    app.router.add_get("/download/{file_id}/{tail:.*}", download)
