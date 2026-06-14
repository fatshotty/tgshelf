"""Manual smoke test of the real Telegram layer (upload / ranged download / move).

Exercises TgClient's raw methods that the FakeGateway can't: SaveBigFilePart,
SendMedia, ranged GetFile (with DC senders), copy_message, delete_message.

Usage:
  # 1) discover your channel ids (the account must be a member/admin):
  python scripts/smoke.py --list-channels

  # 2) run the end-to-end test against two of your channels:
  python scripts/smoke.py <channel_a> <channel_b>

  # optional flags:
  #   --config PATH     (default ./config.yaml)
  #   --size-mb N       size of the Telegram-backed test file (default 3)
  #   --keep            don't purge the test nodes/messages at the end

Nothing is written outside a "/__smoke__" folder in the drive; with default
(no --keep) everything created is purged (DB rows + Telegram messages) at the end.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time


def _rate(nbytes: int, seconds: float) -> str:
    mb = nbytes / (1024 * 1024)
    return f"{mb:.1f} MB in {seconds:.2f}s = {mb / seconds:.2f} MB/s" if seconds > 0 else "instant"

# make `src/` importable when run from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tgshelf.config import load_config  # noqa: E402
from tgshelf.constants import ROOT_ID  # noqa: E402
from tgshelf.log import setup_logging  # noqa: E402
from tgshelf.core.fs import FileSystem  # noqa: E402
from tgshelf.db.engine import create_engine, create_session_factory  # noqa: E402
from tgshelf.db.repo import NodeRepo  # noqa: E402
from tgshelf.http.serve import build_runtime, make_rate_limiter, start_clients  # noqa: E402


def log(step: str) -> None:
    print(f"\n=== {step} ===", flush=True)


async def list_channels(config) -> None:
    rate = make_rate_limiter(config.telegram.rate_limit)
    clients = await start_clients(config, rate)
    if not clients:
        print("no connected accounts (run `tgshelf accounts login <name>`)")
        return
    _account, tg = clients[0]
    tele = tg._client  # the underlying Telethon client
    print("channels visible to the first account:")
    async for dialog in tele.iter_dialogs():
        if dialog.is_channel:
            print(f"  {dialog.id:>16}  {dialog.title}")
    await tele.disconnect()


async def run_smoke(config, channel_a, channel_b, size_mb, keep, file_path) -> None:
    rate = make_rate_limiter(config.telegram.rate_limit)
    clients = await start_clients(config, rate)
    if not clients:
        print("no connected accounts; run `tgshelf accounts login <name>` first")
        return
    _account, user_client = clients[0]

    engine = create_engine(config.db)
    factory = create_session_factory(engine)

    async def fresh_fs(session):
        return FileSystem(
            NodeRepo(session),
            master_channel=config.telegram.upload.channel,
            gateway=user_client,
            uploader=runtime["uploader"],
            streamer=runtime["streamer"],
            min_size=config.telegram.upload.min_size,
        )

    runtime = build_runtime(config, factory, clients)
    created_ids: list[str] = []
    try:
        async with factory() as session:
            repo = NodeRepo(session)
            await repo.bootstrap_root()
            await session.commit()
            fs = await fresh_fs(session)

            leftover = await fs.resolve("/__smoke__")
            if leftover is not None:
                log("reset: purge leftover /__smoke__ from a previous run")
                await fs.delete(leftover.id, purge=True)
                print("removed leftover /__smoke__")

            log("setup: /__smoke__/A (channel A) and /__smoke__/B (channel B)")
            base = await fs.mkdirs("/__smoke__")
            folder_a = await fs.mkdir(base.id, "A")
            await fs.set_channel(folder_a.id, channel_a)
            folder_b = await fs.mkdir(base.id, "B")
            await fs.set_channel(folder_b.id, channel_b)
            created_ids.append(base.id)
            print(f"A -> channel {channel_a}, B -> channel {channel_b}")

            # -- inline (small) ------------------------------------------------
            log("upload small file (inline, stays in DB)")
            small = b"hello tgshelf inline\n"
            n1 = await fs.write(folder_a.id, "small.txt", _src(small))
            back = await _read(fs, n1.id)
            assert back == small, "inline roundtrip mismatch"
            print(f"OK inline: {len(small)} bytes, node {n1.id}, parts={len(await fs.repo.parts_of(n1.id))}")

            # -- telegram parts ------------------------------------------------
            if file_path:
                name = os.path.basename(file_path)
                size = os.path.getsize(file_path)
                source = _file_src(file_path)
                expected_hash = await asyncio.to_thread(_file_hash, file_path)

                def slice_expected(s, e):
                    with open(file_path, "rb") as f:
                        f.seek(s)
                        return f.read(e - s + 1)
            else:
                payload = os.urandom(size_mb * 1024 * 1024)
                name, size = "blob.bin", len(payload)
                source = _src(payload, chunk=256 * 1024)
                expected_hash = hashlib.sha256(payload).hexdigest()
                slice_expected = lambda s, e: payload[s : e + 1]  # noqa: E731

            log(f"upload '{name}' ({size} bytes) — real SaveBigFilePart + SendMedia")
            t0 = time.perf_counter()
            n2 = await fs.write(folder_a.id, name, source)
            up_dt = time.perf_counter() - t0
            parts = await fs.repo.parts_of(n2.id)
            print(f"OK upload: node {n2.id}, channel {n2.channel_id}, "
                  f"{len(parts)} part(s), messages {[p.message_id for p in parts]}")
            print(f"  UPLOAD SPEED: {_rate(size, up_dt)}")

            log("ranged download (full hash + a middle range)")
            t0 = time.perf_counter()
            got_hash = await _stream_hash(fs, n2.id)
            down_dt = time.perf_counter() - t0
            assert got_hash == expected_hash, "full download hash mismatch"
            print(f"  DOWNLOAD SPEED: {_rate(size, down_dt)}")
            rs = min(1_000_000, max(0, size // 4))
            re = min(rs + 500_000, size - 1)
            got = await _read(fs, n2.id, start=rs, end=re)
            assert got == slice_expected(rs, re), "ranged download mismatch"
            print(f"OK download: full hash matches + range {rs}-{re} correct")

            # -- move across channels -----------------------------------------
            log(f"move '{name}' from channel A to channel B "
                f"({len(parts)} part(s) — server-side copy, no byte transfer)")
            old_msgs = [p.message_id for p in parts]
            t0 = time.perf_counter()
            await fs.move(n2.id, folder_b.id)
            move_dt = time.perf_counter() - t0
            print(f"  MOVE TIME: {move_dt:.2f}s for {len(parts)} part(s) "
                  f"({move_dt / max(len(parts), 1):.2f}s/part)")
            moved = await fs.get(n2.id)
            new_parts = await fs.repo.parts_of(n2.id)
            assert moved.channel_id == channel_b, "move did not change channel"
            assert await _stream_hash(fs, n2.id) == expected_hash, "post-move download hash mismatch"
            print(f"OK move: now channel {moved.channel_id}, "
                  f"new messages {[p.message_id for p in new_parts]} (old {old_msgs} deleted)")

            print("\nALL CHECKS PASSED ✅  (verify the messages in your Telegram channels)")
    finally:
        if not keep and created_ids:
            log("cleanup: purge /__smoke__ (DB + Telegram messages)")
            async with factory() as session:
                fs = await fresh_fs(session)
                for nid in created_ids:
                    try:
                        await fs.delete(nid, purge=True)
                        print(f"purged {nid}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"cleanup of {nid} failed: {exc}")
        elif keep:
            print("\n--keep: leaving the test data in place for inspection")
        await engine.dispose()
        await user_client._client.disconnect()


def _src(data: bytes, *, chunk: int = 65536):
    def factory():
        async def gen():
            for i in range(0, len(data), chunk):
                yield data[i : i + chunk]

        return gen()

    return factory


def _file_src(path: str, *, chunk: int = 256 * 1024):
    def factory():
        async def gen():
            with open(path, "rb") as f:
                while True:
                    block = await asyncio.to_thread(f.read, chunk)
                    if not block:
                        break
                    yield block

        return gen()

    return factory


def _file_hash(path: str, *, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


async def _read(fs, node_id, start=0, end=None) -> bytes:
    out = bytearray()
    async for c in fs.open_read(node_id, start, end):
        out.extend(c)
    return bytes(out)


async def _stream_hash(fs, node_id) -> str:
    h = hashlib.sha256()
    async for c in fs.open_read(node_id):
        h.update(c)
    return h.hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("channels", nargs="*", type=int, help="two channel ids: A B")
    p.add_argument("--config", default="./config.yaml")
    p.add_argument("--list-channels", action="store_true")
    p.add_argument("--size-mb", type=int, default=3, help="generated blob size (ignored with --file)")
    p.add_argument("--file", help="upload this real file instead of a random blob")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--log", default="debug", help="log level: no|error|warn|info|debug")
    args = p.parse_args()

    setup_logging(args.log)  # make the app's logging visible while smoke testing
    config = load_config(args.config)
    if args.list_channels:
        asyncio.run(list_channels(config))
        return
    if len(args.channels) < 2:
        p.error("provide two channel ids (or use --list-channels). Example: smoke.py -100123 -100456")
    if args.file and not os.path.isfile(args.file):
        p.error(f"file not found: {args.file}")
    asyncio.run(
        run_smoke(config, args.channels[0], args.channels[1], args.size_mb, args.keep, args.file)
    )


if __name__ == "__main__":
    main()
