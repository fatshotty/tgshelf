"""YAML configuration loading and validation.

The whole tree is frozen dataclasses: components receive a `Config` and can
rely on every field being present, typed and validated. Required keys missing
or invalid values raise `ConfigError` at startup, never at request time.
"""

from __future__ import annotations

import ipaddress
import os
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from tgshelf.constants import PART_SIZE

LOG_LEVELS = ("no", "error", "warn", "info", "debug")
SESSION_STORAGES = ("db", "file")
STRM_PLACEHOLDERS = ("file_id", "filename", "channel_id", "parts", "size", "mime")


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class AccountConfig:
    name: str
    api_id: int
    api_hash: str
    bot_token: str | None = None

    @property
    def is_bot(self) -> bool:
        return self.bot_token is not None


@dataclass(frozen=True)
class UploadConfig:
    channel: int
    min_size: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class NotifyConfig:
    channel: int | None = None


@dataclass(frozen=True)
class TelegramConfig:
    upload: UploadConfig
    notify: NotifyConfig = NotifyConfig()
    users: tuple[AccountConfig, ...] = ()


@dataclass(frozen=True)
class DownloadConfig:
    multi_bot_download: int = 1
    allow_user_fallback: bool = False
    chunk_timeout: float = 6.0
    # Soft RAM threshold (bytes) above which NEW streams start sequential (K=1)
    # instead of multi-bot. 0 = disabled. Streams are never refused or queued.
    memory_soft_limit: int = 0


@dataclass(frozen=True)
class OperationsConfig:
    concurrent: int = 3
    sleep: float = 1.0
    batch: int = 10


@dataclass(frozen=True)
class HttpConfig:
    enabled: bool = True
    host: str = "127.0.0.1" 
    port: int = 3000
    user: str = ""
    password: str = ""
    ignore_auth_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrmConfig:
    destination: str = "./strm"
    source: str = "/"
    template: str = "http://127.0.0.1:3000/files/{file_id}"
    clear_folder: bool = False


@dataclass(frozen=True)
class ChangesFeedConfig:
    enabled: bool = False
    retention_days: int = 7


@dataclass(frozen=True)
class Config:
    db: str
    telegram: TelegramConfig
    data: str = "./data"
    logger: str = "info"
    session_storage: str = "db"
    download: DownloadConfig = DownloadConfig()
    operations: OperationsConfig = OperationsConfig()
    http: HttpConfig = HttpConfig()
    strm: StrmConfig = StrmConfig()
    changes_feed: ChangesFeedConfig = ChangesFeedConfig()


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"'{key}' must be a mapping")
    return value


def _int(section: Mapping[str, Any], key: str, default: int, *, path: str) -> int:
    value = section.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{path}.{key}' must be an integer, got {value!r}") from None


def _float(section: Mapping[str, Any], key: str, default: float, *, path: str) -> float:
    value = section.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"'{path}.{key}' must be a number, got {value!r}") from None


def _bool(section: Mapping[str, Any], key: str, default: bool, *, path: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{path}.{key}' must be a boolean, got {value!r}")
    return value


def _str(section: Mapping[str, Any], key: str, default: str, *, path: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"'{path}.{key}' must be a string, got {value!r}")
    return value


def _parse_accounts(raw_users: Any) -> tuple[AccountConfig, ...]:
    if raw_users is None:
        return ()
    if not isinstance(raw_users, list):
        raise ConfigError("'telegram.users' must be a list")
    accounts: list[AccountConfig] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw_users):
        if not isinstance(entry, Mapping):
            raise ConfigError(f"'telegram.users[{i}]' must be a mapping")
        path = f"telegram.users[{i}]"
        for required in ("name", "api_id", "api_hash"):
            if required not in entry:
                raise ConfigError(f"'{path}' is missing required key '{required}'")
        name = str(entry["name"])
        if name in seen:
            raise ConfigError(f"duplicate account name '{name}' in telegram.users")
        seen.add(name)
        bot_token = entry.get("bot_token")
        accounts.append(
            AccountConfig(
                name=name,
                api_id=_int(entry, "api_id", 0, path=path),
                api_hash=str(entry["api_hash"]),
                bot_token=str(bot_token) if bot_token is not None else None,
            )
        )
    return tuple(accounts)


def _parse_telegram(raw: Mapping[str, Any]) -> TelegramConfig:
    section = _section(raw, "telegram")
    upload = _section(section, "upload")

    if "channel" not in upload:
        raise ConfigError("'telegram.upload.channel' is required (master channel)")
    channel = _int(upload, "channel", 0, path="telegram.upload")

    min_size = _int(upload, "min_size", UploadConfig.min_size, path="telegram.upload")
    if min_size < 0 or min_size % PART_SIZE != 0:
        raise ConfigError(
            f"'telegram.upload.min_size' must be a non-negative multiple of {PART_SIZE}, got {min_size}"
        )

    notify = _section(section, "notify")
    notify_channel = (
        _int(notify, "channel", 0, path="telegram.notify") if "channel" in notify else None
    )

    return TelegramConfig(
        upload=UploadConfig(channel=channel, min_size=min_size),
        notify=NotifyConfig(channel=notify_channel),
        users=_parse_accounts(section.get("users")),
    )


def _parse_download(raw: Mapping[str, Any]) -> DownloadConfig:
    section = _section(raw, "download")
    cfg = DownloadConfig(
        multi_bot_download=_int(
            section, "multi_bot_download", DownloadConfig.multi_bot_download, path="download"
        ),
        allow_user_fallback=_bool(
            section, "allow_user_fallback", DownloadConfig.allow_user_fallback, path="download"
        ),
        chunk_timeout=_float(section, "chunk_timeout", DownloadConfig.chunk_timeout, path="download"),
        memory_soft_limit=_int(
            section, "memory_soft_limit", DownloadConfig.memory_soft_limit, path="download"
        ),
    )
    if cfg.multi_bot_download < 1:
        raise ConfigError("'download.multi_bot_download' must be >= 1")
    if cfg.chunk_timeout <= 0:
        raise ConfigError("'download.chunk_timeout' must be > 0")
    if cfg.memory_soft_limit < 0:
        raise ConfigError("'download.memory_soft_limit' must be >= 0 (0 = disabled)")
    return cfg


def _parse_http(raw: Mapping[str, Any]) -> HttpConfig:
    section = _section(raw, "http")
    networks = section.get("ignore_auth_for") or []
    if not isinstance(networks, list):
        raise ConfigError("'http.ignore_auth_for' must be a list of CIDR strings")
    for net in networks:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            raise ConfigError(f"'http.ignore_auth_for' contains an invalid network: {net!r}") from None
    return HttpConfig(
        enabled=_bool(section, "enabled", HttpConfig.enabled, path="http"),
        host=_str(section, "host", HttpConfig.host, path="http"),
        port=_int(section, "port", HttpConfig.port, path="http"),
        user=_str(section, "user", HttpConfig.user, path="http"),
        password=_str(section, "pass", HttpConfig.password, path="http"),
        ignore_auth_for=tuple(str(n) for n in networks),
    )


def _parse_strm(raw: Mapping[str, Any]) -> StrmConfig:
    section = _section(raw, "strm")
    template = _str(section, "template", StrmConfig.template, path="strm")
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None and field_name not in STRM_PLACEHOLDERS:
            raise ConfigError(
                f"'strm.template' has an unknown placeholder {{{field_name}}}; "
                f"valid placeholders: {', '.join(STRM_PLACEHOLDERS)}"
            )
    return StrmConfig(
        destination=_str(section, "destination", StrmConfig.destination, path="strm"),
        source=_str(section, "source", StrmConfig.source, path="strm"),
        template=template,
        clear_folder=_bool(section, "clear_folder", StrmConfig.clear_folder, path="strm"),
    )


def _parse_changes_feed(raw: Mapping[str, Any]) -> ChangesFeedConfig:
    section = _section(raw, "changes_feed")
    return ChangesFeedConfig(
        enabled=_bool(section, "enabled", ChangesFeedConfig.enabled, path="changes_feed"),
        retention_days=_int(
            section, "retention_days", ChangesFeedConfig.retention_days, path="changes_feed"
        ),
    )


def _parse_operations(raw: Mapping[str, Any]) -> OperationsConfig:
    section = _section(raw, "operations")
    return OperationsConfig(
        concurrent=_int(section, "concurrent", OperationsConfig.concurrent, path="operations"),
        sleep=_float(section, "sleep", OperationsConfig.sleep, path="operations"),
        batch=_int(section, "batch", OperationsConfig.batch, path="operations"),
    )


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate the YAML config file.

    The `DB` environment variable overrides the `db` key (docker/multi-server
    deployments share the same config file but may point at different DSNs).
    """
    if env is None:
        env = os.environ
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be a YAML mapping")

    db = env.get("DB") or raw.get("db")
    if not db:
        raise ConfigError("'db' is required (or set the DB environment variable)")

    logger = _str(raw, "logger", "info", path="<root>")
    if logger not in LOG_LEVELS:
        raise ConfigError(f"'logger' must be one of {LOG_LEVELS}, got {logger!r}")

    session_storage = _str(raw, "session_storage", "db", path="<root>")
    if session_storage not in SESSION_STORAGES:
        raise ConfigError(
            f"'session_storage' must be one of {SESSION_STORAGES}, got {session_storage!r}"
        )

    return Config(
        db=str(db),
        telegram=_parse_telegram(raw),
        data=_str(raw, "data", "./data", path="<root>"),
        logger=logger,
        session_storage=session_storage,
        download=_parse_download(raw),
        operations=_parse_operations(raw),
        http=_parse_http(raw),
        strm=_parse_strm(raw),
        changes_feed=_parse_changes_feed(raw),
    )
