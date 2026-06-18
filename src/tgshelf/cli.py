"""Command line interface.

Skeleton for now: the full command surface from the plan is registered so the
UX is stable from day one; each command is wired to its implementation as the
corresponding task lands. Unimplemented commands exit with code 2.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from tgshelf import __version__
from tgshelf.config import Config, ConfigError, load_config
from tgshelf.log import setup_logging

# command -> (help, implementing task)
COMMANDS: dict[str, tuple[str, str]] = {
    "serve": ("run the HTTP server (API + streaming)", "B1"),
    "sync": ("upload a local directory tree to the drive", "C3"),
    "strm": ("generate .strm files from the virtual filesystem", "C3"),
    "download": ("download a file/folder from the drive to local disk", "C-download"),
    "accounts": ("manage Telegram accounts/sessions (login, add-bot, list)", "A3"),
    "create-bots": ("create bots via BotFather and join them to channels", "C4"),
    "bots": ("check/repair bot membership on all channels in use", "C4"),
    "mkdir": ("create a folder (with parents) in the virtual filesystem", "C2"),
    "ls": ("list a folder of the virtual filesystem", "C2"),
    "cp": ("copy files/folders", "C2"),
    "mv": ("move files/folders", "C2"),
    "rm": ("delete files/folders (soft delete)", "C2"),
    "purge": ("permanently delete soft-deleted items", "C2"),
    "import-channel": ("reconcile/catalog a channel history into the drive", "C5"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgshelf",
        description="Telegram as cloud storage: virtual filesystem, HTTP streaming proxy",
    )
    parser.add_argument("--version", action="version", version=f"tgshelf {__version__}")
    parser.add_argument(
        "--config",
        default="./config.yaml",
        help="path to the YAML config file (default: ./config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, (help_text, _task) in COMMANDS.items():
        cmd = subparsers.add_parser(name, help=help_text)
        if name == "accounts":
            _add_accounts_subparsers(cmd)
        elif name in ("ls", "rm", "purge", "mkdir"):
            cmd.add_argument("path", help="path of a folder or file")
        elif name in ("cp", "mv"):
            cmd.add_argument("src", help="source path (file or folder)")
            cmd.add_argument("dst", help="destination folder path")
        elif name == "strm":
            cmd.add_argument("--source", help="drive folder to mirror (default: config strm.source)")
            cmd.add_argument("--destination", help="local output folder (default: config strm.destination)")
            cmd.add_argument("--clear", action="store_true", help="wipe destination first (full regen)")
        elif name == "sync":
            cmd.add_argument("local_dir", help="local directory to upload")
            cmd.add_argument("--dest", help="drive destination folder (default: /)")
            cmd.add_argument("--concurrent", type=int, help="parallel uploads (default: config operations.concurrent)")
            cmd.add_argument("--delete-source", action="store_true", dest="delete_source",
                             help="delete each uploaded file from disk (+ prune emptied dirs, not the root)")
            cmd.add_argument("--overwrite", action="store_true",
                             help="re-upload and replace an existing drive file (default: skip)")
        elif name == "download":
            cmd.add_argument("path", help="drive path of a file or folder to download")
            cmd.add_argument("--dest", help="local destination dir (default: cwd)")
            cmd.add_argument("--concurrent", type=int,
                             help="parallel files (default: config operations.concurrent)")
            cmd.add_argument("--overwrite", action="store_true",
                             help="re-download from scratch (ignore skip/resume)")
            cmd.add_argument("--log-file", dest="log_file",
                             help="errors log file (default: <dest>/tgshelf-download-errors.log)")
        elif name == "create-bots":
            cmd.add_argument("--prefix", required=True, help="bot username prefix (e.g. 'redstream' -> redstream_01_bot)")
            cmd.add_argument("--count", type=int, required=True, help="how many bots to create")
            cmd.add_argument("--start", type=int, default=1, help="first index (default: 1)")
            cmd.add_argument("--delay", type=int, default=5, help="seconds between creations (default: 5)")
        elif name == "bots":
            _add_bots_subparsers(cmd)
        elif name == "import-channel":
            cmd.add_argument(
                "--limit", type=int, default=0,
                help="scan only the last N messages (0 = whole history, default)",
            )
    return parser


def _add_bots_subparsers(cmd: argparse.ArgumentParser) -> None:
    sub = cmd.add_subparsers(dest="bots_cmd", required=True)
    sub.add_parser("check", help="verify/repair bot membership on all channels in use")


def _add_accounts_subparsers(cmd: argparse.ArgumentParser) -> None:
    sub = cmd.add_subparsers(dest="accounts_cmd", required=True)
    sub.add_parser("list", help="list configured accounts and session status")
    login = sub.add_parser("login", help="interactive user login")
    login.add_argument("name", help="account name from config")
    add_bot = sub.add_parser("add-bot", help="register a bot from its config token")
    add_bot.add_argument("name", help="bot account name from config")
    imp = sub.add_parser("import", help="import a legacy Telethon .session file")
    imp.add_argument("name", help="account name from config")
    imp.add_argument("--session", required=True, help="path to the .session file")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config.logger)

    return _dispatch(config, args)


def _dispatch(config: Config, args: argparse.Namespace) -> int:
    if args.command == "accounts":
        from tgshelf.commands import accounts

        return asyncio.run(accounts.run(config, args))

    if args.command in ("mkdir", "ls", "cp", "mv", "rm", "purge"):
        from tgshelf.commands import fsops

        return asyncio.run(fsops.run(config, args))

    if args.command == "strm":
        from tgshelf.commands import strm

        return asyncio.run(strm.run(config, args))

    if args.command == "sync":
        from tgshelf.commands import sync

        return asyncio.run(sync.run(config, args))

    if args.command == "download":
        from tgshelf.commands import download

        return asyncio.run(download.run(config, args))

    if args.command == "create-bots":
        from tgshelf.commands import bots

        return asyncio.run(bots.run_create(config, args))

    if args.command == "bots":
        from tgshelf.commands import bots

        if args.bots_cmd == "check":
            return asyncio.run(bots.run_check(config, args))
        print(f"error: unknown bots subcommand {args.bots_cmd!r}", file=sys.stderr)
        return 2

    if args.command == "import-channel":
        from tgshelf.commands import import_channel

        return asyncio.run(import_channel.run(config, args))

    if args.command == "serve":
        from tgshelf.http.serve import ServeError, run_server

        try:
            asyncio.run(run_server(config))
        except ServeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    _help_text, task = COMMANDS[args.command]
    print(
        f"tgshelf {args.command}: not implemented yet (scheduled for task {task})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
