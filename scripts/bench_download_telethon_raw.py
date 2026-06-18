"""A/B/C decisive experiment — Telethon, single bot, ONE variable: which sender.

The legacy/current comparison left one question: current (Telethon) provokes
~1181 FloodWaits, legacy (Pyrogram) zero, same bot/file/DC. The fetch-slow lines
showed the file is on the bot's HOME DC, so current downloads on the MAIN
connection (`client._sender`) — exactly what Telethon's own _DirectDownloadIter
does for a home-DC file. Pyrogram instead always opens a DEDICATED `is_media`
connection (even for the home DC) and gets full bandwidth.

This bench isolates THAT single variable inside Telethon:

  --via main   GetFile on client._sender  (the main connection)  -> reproduce floods
  --via media  GetFile on a dedicated sender to the home DC      -> expect ~0 floods

The dedicated sender mirrors Telethon's `_create_exported_sender` MINUS the
export/import auth (same DC + same auth_key => no ExportAuthorization, which is
what raised DcIdInvalidError and pushed the current code onto the main
connection). If `--via media` floods drop to ~0, the fix is proven: open one
media connection per stream and read every chunk on it.

RUN WITH THE TGSHELF VENV (on the same server as the other benches):
  .venv/bin/python scripts/bench_download_telethon_raw.py --via media \
      --channel -100... --messages 31,32,33,34 \
      --api-id .. --api-hash .. --bot-token .. --log-file /tmp/bench_media.log
  .venv/bin/python scripts/bench_download_telethon_raw.py --via main  ... (reproduce)

Tip: validate quickly first with `--max-chunks 300` (each mode), then full run.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import logging
import time

from telethon import TelegramClient, functions
from telethon import errors as tg_errors
from telethon.network import MTProtoSender
from telethon.sessions import StringSession
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions.upload import GetFileRequest
from telethon.tl.types import InputDocumentFileLocation

CHUNK = 1024 * 1024  # 1 MiB — identical to both other benches

log = logging.getLogger("raw.bench")


async def _dedicated_media_sender(client: TelegramClient, dc_id: int) -> MTProtoSender:
    """A fresh MTProtoSender to the dc's MEDIA endpoint (media_only=True), reusing
    the client's existing auth_key (valid because it's the HOME DC) — no
    ExportAuthorization. This is what Pyrogram's is_media session does and what
    Telethon does NOT: Telethon's _get_dc always returns the regular endpoint,
    which Telegram throttles for sustained GetFile. We init the fresh connection
    (InvokeWithLayer + initConnection) so Telegram accepts requests."""
    cfg = await client(functions.help.GetConfigRequest())
    opts = [o for o in cfg.dc_options
            if o.id == dc_id and not o.cdn and bool(o.ipv6) == client._use_ipv6]
    media = next((o for o in opts if o.media_only), None)
    chosen = media or (opts[0] if opts else await client._get_dc(dc_id))
    log.info("[raw] dc %d endpoint: ip=%s port=%s media_only=%s",
             dc_id, chosen.ip_address, chosen.port, getattr(chosen, "media_only", None))
    sender = MTProtoSender(client.session.auth_key, loggers=client._log)
    await sender.connect(client._connection(
        chosen.ip_address, chosen.port, dc_id,
        loggers=client._log, proxy=client._proxy, local_addr=client._local_addr,
    ))
    sender.dc_id = dc_id
    # a fresh connection must carry initConnection on its first call
    init = copy.copy(client._init_request)
    init.query = functions.help.GetConfigRequest()
    await sender.send(functions.InvokeWithLayerRequest(LAYER, init))
    return sender


async def _resolve(client, channel, mid):
    msg = await client.get_messages(channel, ids=mid)
    doc = getattr(msg, "document", None)
    if doc is None:
        raise SystemExit(f"message {mid} has no document")
    loc = InputDocumentFileLocation(
        id=doc.id, access_hash=doc.access_hash,
        file_reference=doc.file_reference, thumb_size="",
    )
    return loc, doc.dc_id, doc.size


async def run(args) -> None:
    client = TelegramClient(
        StringSession(), args.api_id, args.api_hash,
        receive_updates=False, flood_sleep_threshold=0,  # surface floods, we count them
    )
    await client.start(bot_token=args.bot_token)
    home_dc = client.session.dc_id
    log.info("[raw] bot home dc = %s; mode = via %s", home_dc, args.via)

    channel = args.channel
    msg_ids = [int(m) for m in args.messages.split(",") if m.strip()]
    parts = []
    for mid in msg_ids:
        loc, dc_id, size = await _resolve(client, channel, mid)
        parts.append((mid, loc, dc_id, size))
        log.info("[raw] part msg=%s dc=%s size=%d", mid, dc_id, size)

    # pick the sender once
    file_dc = parts[0][2]
    if args.via == "media":
        sender = await _dedicated_media_sender(client, file_dc)
    else:
        sender = client._sender  # the main connection (reproduces current)
        log.info("[raw] using MAIN connection (client._sender)")

    total = sum(p[3] for p in parts)
    log.info("[raw] START via=%s total=%d B", args.via, total)

    got = seq = floods = 0
    flood_time = 0.0
    t0 = time.monotonic()
    try:
        for p_idx, (mid, loc, dc_id, size) in enumerate(parts):
            offset = 0
            while True:
                fl = 0.0
                while True:  # ride FloodWaits inline, same offset, same sender
                    t = time.monotonic()
                    try:
                        res = await sender.send(GetFileRequest(
                            location=loc, offset=offset, limit=CHUNK, precise=False))
                        dt = time.monotonic() - t
                        break
                    except tg_errors.FloodWaitError as e:
                        fl += e.seconds
                        log.warning("[flood] FloodWait %ds (chunk %d off=%d); sleeping inline",
                                    e.seconds, seq, offset)
                        await asyncio.sleep(e.seconds)
                n = len(res.bytes)
                got += n
                if fl > 0:
                    floods += 1
                    flood_time += fl
                log.debug("[fetch] chunk %d part %d (msg %s, dc %s) <- 'raw' (%d B) in %.2fs off=%d",
                          seq, p_idx, mid, dc_id, n, dt, offset)
                seq += 1
                offset += CHUNK
                if n < CHUNK or (size and offset >= size):
                    break
                if args.max_chunks and seq >= args.max_chunks:
                    raise StopIteration
    except StopIteration:
        log.info("[raw] stopped early at --max-chunks %d", args.max_chunks)
    finally:
        dt = time.monotonic() - t0
        avg = (got / (1024 * 1024) / dt) if dt > 0 else 0.0
        recap = (
            f"RECAP path=raw-{args.via} bytes={got} time={dt:.2f} avg={avg:.2f}MB/s "
            f"chunks={seq} quarantined=False consecutive_errors=0 "
            f"cooldown_remaining=0.0 floods={floods} flood_time={flood_time:.1f}"
        )
        log.info("%s", recap)
        await client.disconnect()


def main() -> None:
    p = argparse.ArgumentParser(description="Telethon main-vs-media-sender download experiment")
    p.add_argument("--via", choices=("main", "media"), required=True)
    p.add_argument("--channel", required=True, type=int)
    p.add_argument("--messages", required=True, help="ordered message ids, comma-separated")
    p.add_argument("--api-id", required=True, type=int)
    p.add_argument("--api-hash", required=True)
    p.add_argument("--bot-token", required=True)
    p.add_argument("--max-chunks", type=int, default=0, help="stop after N chunks (quick probe)")
    p.add_argument("--log", default="debug")
    p.add_argument("--log-file")
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
    logging.getLogger("telethon").setLevel(logging.WARNING)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
