"""YAML configuration loading and validation.

The whole tree is frozen dataclasses: components receive a `Config` and can
rely on every field being present, typed and validated. Required keys missing
or invalid values raise `ConfigError` at startup, never at request time.
"""

from __future__ import annotations

import ipaddress
import os
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from tgshelf.constants import PART_SIZE
from tgshelf.core.captions import (
    CAPTION_PLACEHOLDERS,
    DEFAULT_CAPTION_TEMPLATE,
    RESERVED_CAPTION_PLACEHOLDERS,
)

LOG_LEVELS = ("no", "error", "warn", "info", "debug")
SESSION_STORAGES = ("db", "file")
STRM_PLACEHOLDERS = ("file_id", "filename", "channel_id", "parts", "parts_dash", "size", "mime")
ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
NOTIFY_TEMPLATE = """[tgshelf:{severity}] {title}

Impact: {impact}
Scope: {scope}
File: {file_path}
Node: {node_id}
Part: {part_idx}
Channel: {channel_id}
Account: {account}
Cause: {cause}
Action: {action}
Time: {time}
Host: {host}
Key: {key}"""


class ConfigError(Exception):
    """Invalid or missing configuration."""


def _child_path(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    if path == "<root>":
        return str(key)
    return f"{path}.{key}"


def resolve_env_refs(value: Any, env: Mapping[str, str], *, path: str = "<root>") -> Any:
    """Expand ${VAR} references in YAML scalar strings."""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise ConfigError(
                    f"'{path}' references missing environment variable {name}"
                )
            return env[name]

        return ENV_REF_PATTERN.sub(replace, value)

    if isinstance(value, list):
        return [
            resolve_env_refs(item, env, path=_child_path(path, i))
            for i, item in enumerate(value)
        ]

    if isinstance(value, Mapping):
        return {
            key: resolve_env_refs(item, env, path=_child_path(path, key))
            for key, item in value.items()
        }

    return value


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
    bot_token: str | None = None
    channel: int | str | None = None
    template: str = NOTIFY_TEMPLATE
    warning_window: float = 300.0


@dataclass(frozen=True)
class MainBotConfig:
    """A dedicated, brand-new bot (its own BotFather token) that watches the
    master channel live. NOT one of the pool `users`: it is a separate instance
    with receive_updates=True, started only by `serve`. The operator must add it
    as admin of the master channel."""

    api_id: int
    api_hash: str
    bot_token: str


@dataclass(frozen=True)
class TelegramConfig:
    upload: UploadConfig
    notify: NotifyConfig = NotifyConfig()
    users: tuple[AccountConfig, ...] = ()
    # optional dedicated watcher bot (own token, outside `users`); None = disabled
    main_bot: MainBotConfig | None = None


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
    actions: int = 0
    within: float = 40.0


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
class RcloneConfig:
    """rclone integration: a read-write WebDAV data-plane (`/dav`) plus a
    push-based control-plane that invalidates rclone's VFS directory cache the
    instant any writer touches the tree (changes feed → `vfs/forget`).

    No rc endpoint lives here (user decision): the rclone client declares its own
    rc URL at mount time via the `X-Tgshelf-RC` header and is kept in an in-memory
    registry. `register_token` (shared secret) authorises that self-registration;
    empty = self-registration disabled (safe default). `allowed_rc_networks` is an
    extra CIDR allowlist for the declared rc host (anti-SSRF); the request's own
    source IP is always allowed."""

    webdav_enabled: bool = False
    bridge_enabled: bool = False
    register_token: str = ""
    allowed_rc_networks: tuple[str, ...] = ()
    registry_ttl: int = 600


@dataclass(frozen=True)
class CaptionConfig:
    template: str = DEFAULT_CAPTION_TEMPLATE


@dataclass(frozen=True)
class PluginsConfig:
    enabled: bool = False
    paths: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    db: str
    telegram: TelegramConfig
    data: str = "./data"
    logger: str = "info"
    session_storage: str = "db"
    # Global: TCP connections (MTProtoSenders) each client opens per DC for the
    # high-volume data path (upload SaveBigFilePart / download GetFile), with
    # round-robin. Telegram throttles per-connection; >1 raises throughput.
    # Normalized to max(1, N): 0 and 1 both mean "off" (one connection).
    concurrent_tcp_connections: int = 1
    download: DownloadConfig = DownloadConfig()
    operations: OperationsConfig = OperationsConfig()
    http: HttpConfig = HttpConfig()
    strm: StrmConfig = StrmConfig()
    changes_feed: ChangesFeedConfig = ChangesFeedConfig()
    rclone: RcloneConfig = RcloneConfig()
    caption: CaptionConfig = CaptionConfig()
    plugins: PluginsConfig = PluginsConfig()


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
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    raise ConfigError(f"'{path}.{key}' must be a boolean, got {value!r}")


def _str(section: Mapping[str, Any], key: str, default: str, *, path: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"'{path}.{key}' must be a string, got {value!r}")
    return value


def _str_tuple(section: Mapping[str, Any], key: str, *, path: str) -> tuple[str, ...]:
    value = section.get(key) or []
    if not isinstance(value, list):
        raise ConfigError(f"'{path}.{key}' must be a list of strings")
    result = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"'{path}.{key}[{idx}]' must be a non-empty string")
        result.append(item)
    return tuple(result)


def _notify_channel(value: Any, *, master_channel: int) -> int | str | None:
    if value in (None, ""):
        return master_channel
    if isinstance(value, str) and value.startswith("@"):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(
            f"'telegram.notify.channel' must be an integer channel id or @username, got {value!r}"
        ) from None


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
    notify_window = _float(
        notify, "warning_window", NotifyConfig.warning_window, path="telegram.notify"
    )
    if notify_window <= 0:
        raise ConfigError("'telegram.notify.warning_window' must be > 0")

    if "rate_limit" in section:
        raise ConfigError(
            "'telegram.rate_limit' has been removed; use 'operations.actions' "
            "and 'operations.within' for the per-account Telegram write bucket"
        )

    users = _parse_accounts(section.get("users"))

    raw_main = section.get("main_bot")
    main_bot = None
    if raw_main not in (None, "", {}):
        if not isinstance(raw_main, Mapping):
            raise ConfigError("'telegram.main_bot' must be a mapping")
        for required in ("api_id", "api_hash", "bot_token"):
            if not raw_main.get(required):
                raise ConfigError(f"'telegram.main_bot.{required}' is required")
        main_bot = MainBotConfig(
            api_id=_int(raw_main, "api_id", 0, path="telegram.main_bot"),
            api_hash=str(raw_main["api_hash"]),
            bot_token=str(raw_main["bot_token"]),
        )

    return TelegramConfig(
        upload=UploadConfig(channel=channel, min_size=min_size),
        notify=NotifyConfig(
            bot_token=(
                str(notify["bot_token"])
                if notify.get("bot_token") not in (None, "")
                else None
            ),
            channel=_notify_channel(notify.get("channel"), master_channel=channel),
            template=_str(
                notify, "template", NotifyConfig.template, path="telegram.notify"
            ),
            warning_window=notify_window,
        ),
        users=users,
        main_bot=main_bot,
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


def _parse_caption(raw: Mapping[str, Any]) -> CaptionConfig:
    section = _section(raw, "caption")
    template = _str(section, "template", CaptionConfig.template, path="caption")
    valid = set(CAPTION_PLACEHOLDERS)
    reserved = set(RESERVED_CAPTION_PLACEHOLDERS)
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is None:
            continue
        if field_name in reserved:
            raise ConfigError(
                f"'caption.template' has reserved placeholder {{{field_name}}}; "
                "it is not implemented yet"
            )
        if field_name not in valid:
            raise ConfigError(
                f"'caption.template' has an unknown placeholder {{{field_name}}}; "
                f"valid placeholders: {', '.join(CAPTION_PLACEHOLDERS)}"
            )
    return CaptionConfig(template=template)


def _parse_plugins(raw: Mapping[str, Any]) -> PluginsConfig:
    section = _section(raw, "plugins")
    modules = _str_tuple(section, "modules", path="plugins")
    for idx, spec in enumerate(modules):
        if ":" not in spec:
            raise ConfigError(
                f"'plugins.modules[{idx}]' must use 'module:attribute' syntax"
            )
    return PluginsConfig(
        enabled=_bool(section, "enabled", PluginsConfig.enabled, path="plugins"),
        paths=_str_tuple(section, "paths", path="plugins"),
        modules=modules,
    )


def _parse_changes_feed(raw: Mapping[str, Any]) -> ChangesFeedConfig:
    section = _section(raw, "changes_feed")
    return ChangesFeedConfig(
        enabled=_bool(section, "enabled", ChangesFeedConfig.enabled, path="changes_feed"),
        retention_days=_int(
            section, "retention_days", ChangesFeedConfig.retention_days, path="changes_feed"
        ),
    )


def _parse_rclone(raw: Mapping[str, Any]) -> RcloneConfig:
    section = _section(raw, "rclone")
    networks = section.get("allowed_rc_networks") or []
    if not isinstance(networks, list):
        raise ConfigError("'rclone.allowed_rc_networks' must be a list of CIDR strings")
    for net in networks:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            raise ConfigError(
                f"'rclone.allowed_rc_networks' contains an invalid network: {net!r}"
            ) from None
    cfg = RcloneConfig(
        webdav_enabled=_bool(section, "webdav_enabled", RcloneConfig.webdav_enabled, path="rclone"),
        bridge_enabled=_bool(section, "bridge_enabled", RcloneConfig.bridge_enabled, path="rclone"),
        register_token=_str(section, "register_token", RcloneConfig.register_token, path="rclone"),
        allowed_rc_networks=tuple(str(n) for n in networks),
        registry_ttl=_int(section, "registry_ttl", RcloneConfig.registry_ttl, path="rclone"),
    )
    if cfg.registry_ttl <= 0:
        raise ConfigError("'rclone.registry_ttl' must be > 0")
    return cfg


def _parse_operations(raw: Mapping[str, Any]) -> OperationsConfig:
    section = _section(raw, "operations")
    for legacy_key in ("sleep", "batch"):
        if legacy_key in section:
            raise ConfigError(
                f"'operations.{legacy_key}' has been removed; use "
                "'operations.actions' and 'operations.within' for the "
                "per-account Telegram write bucket"
            )
    cfg = OperationsConfig(
        concurrent=_int(section, "concurrent", OperationsConfig.concurrent, path="operations"),
        actions=_int(section, "actions", OperationsConfig.actions, path="operations"),
        within=_float(section, "within", OperationsConfig.within, path="operations"),
    )
    if cfg.concurrent < 1:
        raise ConfigError("'operations.concurrent' must be >= 1")
    if cfg.actions < 0:
        raise ConfigError("'operations.actions' must be >= 0 (0 = disabled)")
    if cfg.within <= 0:
        raise ConfigError("'operations.within' must be > 0")
    return cfg


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
    if env.get("DB"):
        raw = {**raw, "db": env["DB"]}
    raw = resolve_env_refs(raw, env)

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

    # max(1, N): 0 and 1 both disable the feature (one connection per DC).
    tcp_connections = max(
        1, _int(raw, "concurrent_tcp_connections", 1, path="<root>")
    )

    return Config(
        db=str(db),
        telegram=_parse_telegram(raw),
        data=_str(raw, "data", "./data", path="<root>"),
        logger=logger,
        session_storage=session_storage,
        concurrent_tcp_connections=tcp_connections,
        download=_parse_download(raw),
        operations=_parse_operations(raw),
        http=_parse_http(raw),
        strm=_parse_strm(raw),
        changes_feed=_parse_changes_feed(raw),
        rclone=_parse_rclone(raw),
        caption=_parse_caption(raw),
        plugins=_parse_plugins(raw),
    )
