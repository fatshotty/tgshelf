"""A/B download benchmark — LEGACY path (Pyrogram, single bot, while-True getFile).

Self-contained replica of the legacy download loop so the legacy repo stays
untouched and we drag in no Mongo/configuration coupling. It mirrors VERBATIM:

  - Client(..., in_memory=True, max_concurrent_transmissions=5)
        services/telegram.py:64-73
  - get_media_session(dc): per-DC cached media session + export/import auth
        services/telegram.py:176-226
  - get_file(id, hash, ref, offset, limit=1MiB, dc)
        services/telegram.py:229-249
  - per-part while-True loop, offset += CHUNK, stop on the short last chunk
        services/downloader.py:127-179
  - CHUNK = UPLOAD_CHUNK*2 = 1 MiB  (constants.py:5)

The legacy mitigation for floods is "Pyrogram sleeps the FloodWait inline and
the while-True continues on the SAME media session". We reproduce that exactly
(sleep + retry same offset, same session) but make it visible: sleep_threshold=0
so Pyrogram raises FloodWait, we log `[flood]`, sleep, and retry.

RUN WITH THE LEGACY VENV:
  ~/Sites/telegram-stream/python/venv/bin/python scripts/bench_download_legacy.py \
      --channel -100123 --messages 11,12,13 \
      --api-id 123456 --api-hash 0123abc... --bot-token 123456789:AA... \
      --log-file /tmp/bench_legacy.log
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from pyrogram import Client, raw
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId
from pyrogram.session import Auth, Session

UPLOAD_CHUNK = 512 * 1024
CHUNK = UPLOAD_CHUNK * 2  # 1 MiB — identical to legacy

log = logging.getLogger("legacy.bench")

_MEDIA_TYPES = (
    "audio", "document", "photo", "sticker",
    "animation", "video", "voice", "video_note",
)


def _media_of(message):
    """services/telegram.py:127-146 — first non-None media attr wins."""
    for attr in _MEDIA_TYPES:
        media = getattr(message, attr, None)
        if media:
            fd = FileId.decode(media.file_id)
            return fd
    return None


async def _media_session(api: Client, dc: int) -> Session:
    """services/telegram.py:176-226 — per-DC cached session, export/import auth."""
    ms = api.media_sessions.get(dc, None)
    if ms is not None:
        return ms

    if dc != await api.storage.dc_id():
        log.debug("creating and switch new media_session for %s", dc)
        ms = Session(
            api, dc,
            await Auth(api, dc, await api.storage.test_mode()).create(),
            await api.storage.test_mode(),
            is_media=True,
        )
        await ms.start()
        for _ in range(6):
            exported = await api.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc))
            try:
                await ms.invoke(raw.functions.auth.ImportAuthorization(
                    id=exported.id, bytes=exported.bytes))
                break
            except AuthBytesInvalid:
                log.debug("Invalid authorization bytes for DC %s", dc)
                continue
        else:
            await ms.stop()
            raise AuthBytesInvalid
    else:
        log.debug("creating a new media_session for %s", dc)
        ms = Session(
            api, dc,
            await api.storage.auth_key(),
            await api.storage.test_mode(),
            is_media=True,
        )
        await ms.start()

    api.media_sessions[dc] = ms
    return ms


async def _get_file(api: Client, fd: FileId, offset: int, dc: int) -> bytes:
    """services/telegram.py:229-249 — GetFile on the (foreign) media session."""
    _api = api if dc is None else await _media_session(api, dc)
    location = raw.types.InputDocumentFileLocation(
        id=fd.media_id, access_hash=fd.access_hash,
        file_reference=fd.file_reference, thumb_size="",
    )
    result = await _api.invoke(raw.functions.upload.GetFile(
        location=location, offset=offset, limit=CHUNK, precise=False))
    return result.bytes


async def _get_file_riding_floods(api, fd, offset, dc, bot, seq) -> tuple[bytes, float, float]:
    """Same net behavior as legacy (Pyrogram sleeps the flood, same session
    continues), but logged. Returns (bytes, fetch_dt, flood_seconds)."""
    flood_total = 0.0
    while True:
        t0 = time.monotonic()
        try:
            data = await _get_file(api, fd, offset, dc)
            return data, time.monotonic() - t0, flood_total
        except FloodWait as e:
            secs = int(getattr(e, "value", 0) or 0)
            flood_total += secs
            log.warning("[flood] '%s' FloodWait %ds (chunk %d off=%d); sleeping inline", bot, secs, seq, offset)
            await asyncio.sleep(secs)
            continue


async def run(args) -> None:
    bot = "legacy-bot"
    api = Client(
        name="bench-legacy",
        api_id=args.api_id,
        api_hash=args.api_hash,
        bot_token=args.bot_token,
        no_updates=True,
        in_memory=True,
        max_concurrent_transmissions=5,
        sleep_threshold=0,  # surface floods instead of Pyrogram sleeping silently
    )
    await api.start()

    channel = args.channel
    msg_ids = [int(m) for m in args.messages.split(",") if m.strip()]

    log.info("[bench] resolving %d part(s) on channel %s", len(msg_ids), channel)
    parts = []  # (msg_id, FileId, size)
    for mid in msg_ids:
        message = await api.get_messages(channel, mid)
        fd = _media_of(message)
        if fd is None:
            raise SystemExit(f"message {mid} on {channel} has no downloadable media")
        media = None
        for attr in _MEDIA_TYPES:
            media = getattr(message, attr, None)
            if media:
                break
        size = getattr(media, "file_size", None) or 0
        parts.append((mid, fd, size))
        log.info("[bench] part msg=%s dc=%s size=%d", mid, fd.dc_id, size)

    total = sum(p[2] for p in parts)
    log.info("[bench] START legacy path: bot='%s' single-bot while-True total=%d B", bot, total)

    got = 0
    seq = 0
    floods = 0
    flood_time = 0.0
    t0 = time.monotonic()
    try:
        for p_idx, (mid, fd, size) in enumerate(parts):
            dc = fd.dc_id
            offset = 0
            while True:
                data, dt, fl = await _get_file_riding_floods(api, fd, offset, dc, bot, seq)
                n = len(data)
                got += n
                if fl > 0:
                    floods += 1
                    flood_time += fl
                log.debug(
                    "[fetch] chunk %d part %d (msg %s, dc %s) <- '%s' (%d B) in %.2fs off=%d",
                    seq, p_idx, mid, dc, bot, n, dt, offset,
                )
                seq += 1
                offset += CHUNK
                if n < CHUNK or (size and offset >= size):
                    break
    finally:
        dt = time.monotonic() - t0
        avg = (got / (1024 * 1024) / dt) if dt > 0 else 0.0
        recap = (
            f"RECAP path=legacy bytes={got} time={dt:.2f} avg={avg:.2f}MB/s "
            f"chunks={seq} quarantined=False consecutive_errors=0 "
            f"cooldown_remaining=0.0 floods={floods} flood_time={flood_time:.1f}"
        )
        log.info("%s", recap)  # also lands in --log-file for bench_compare
        await api.stop()


def main() -> None:
    p = argparse.ArgumentParser(description="legacy-path (Pyrogram) download benchmark")
    p.add_argument("--channel", required=True, type=int, help="channel id (e.g. -100123...)")
    p.add_argument("--messages", required=True, help="ordered message ids, comma-separated")
    p.add_argument("--api-id", required=True, type=int)
    p.add_argument("--api-hash", required=True)
    p.add_argument("--bot-token", required=True)
    p.add_argument("--log", default="debug", help="log level: error|warn|info|debug")
    p.add_argument("--log-file", help="also write the log to this file")
    args = p.parse_args()

    level = {"error": logging.ERROR, "warn": logging.WARNING,
             "info": logging.INFO, "debug": logging.DEBUG}.get(args.log, logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
                            datefmt="%d/%m/%Y %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if args.log_file:
        fh = logging.FileHandler(args.log_file, mode="w")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    logging.getLogger("pyrogram").setLevel(logging.WARNING)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
