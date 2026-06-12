"""Command line interface.

Skeleton for now: the full command surface from the plan is registered so the
UX is stable from day one; each command is wired to its implementation as the
corresponding task lands. Unimplemented commands exit with code 2.
"""

from __future__ import annotations

import argparse
import sys

from tgshelf import __version__
from tgshelf.config import ConfigError, load_config
from tgshelf.log import setup_logging

# command -> (help, implementing task)
COMMANDS: dict[str, tuple[str, str]] = {
    "serve": ("run the HTTP server (API + streaming)", "B1"),
    "sync": ("upload a local directory tree to the drive", "C3"),
    "strm": ("generate .strm files from the virtual filesystem", "C3"),
    "accounts": ("manage Telegram accounts/sessions (login, add-bot, list)", "A3"),
    "create-bots": ("create bots via BotFather and join them to channels", "C4"),
    "bots": ("check/repair bot membership on all channels in use", "C4"),
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
        subparsers.add_parser(name, help=help_text)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    setup_logging(config.logger)

    _help_text, task = COMMANDS[args.command]
    print(
        f"tgshelf {args.command}: not implemented yet (scheduled for task {task})",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
