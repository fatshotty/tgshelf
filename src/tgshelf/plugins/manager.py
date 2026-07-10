"""Plugin hook manager."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from tgshelf.plugins.host import PluginHost, PluginNode

log = logging.getLogger("tgshelf.plugins")


class PluginError(Exception):
    """A plugin blocked an operation or failed while handling it."""


@dataclass(frozen=True)
class PluginContext:
    operation: str
    host: PluginHost
    node: PluginNode
    old_parent_id: str | None = None
    new_parent_id: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    source_node: PluginNode | None = None
    target_node: PluginNode | None = None


class PluginManager:
    def __init__(self, plugins: Iterable[Any] = ()):
        self._plugins = tuple(plugins)

    @property
    def enabled(self) -> bool:
        return bool(self._plugins)

    async def run_before(self, hook_name: str, ctx: PluginContext) -> None:
        for plugin in self._plugins:
            hook = getattr(plugin, hook_name, None)
            if hook is None:
                continue
            try:
                await hook(ctx)
            except PluginError:
                raise
            except Exception as exc:  # noqa: BLE001 - plugin boundary
                raise PluginError(
                    f"plugin {_plugin_name(plugin)} failed {hook_name}: {exc}"
                ) from exc

    async def run_after(self, hook_name: str, ctx: PluginContext) -> None:
        for plugin in self._plugins:
            hook = getattr(plugin, hook_name, None)
            if hook is None:
                continue
            try:
                await hook(ctx)
            except Exception as exc:  # noqa: BLE001 - after hooks never roll back
                log.error(
                    "[plugin] %s.%s failed: %s",
                    _plugin_name(plugin),
                    hook_name,
                    exc,
                    exc_info=exc,
                )


def _plugin_name(plugin: Any) -> str:
    return plugin.__class__.__name__
