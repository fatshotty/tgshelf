"""Configuration-driven plugin loading."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from tgshelf.config import ConfigError, PluginsConfig
from tgshelf.plugins.manager import PluginManager


def load_plugins(config: PluginsConfig) -> PluginManager:
    if not config.enabled:
        return PluginManager()

    for raw_path in config.paths:
        path = str(Path(raw_path).expanduser().resolve())
        if path not in sys.path:
            sys.path.insert(0, path)

    plugins = []
    for idx, spec in enumerate(config.modules):
        module_name, attr_name = spec.split(":", 1)
        if not module_name or not attr_name:
            raise ConfigError(
                f"'plugins.modules[{idx}]' must use 'module:attribute' syntax"
            )
        try:
            module = importlib.import_module(module_name)
            target = getattr(module, attr_name)
        except (ImportError, AttributeError) as exc:
            raise ConfigError(f"could not load plugin '{spec}': {exc}") from exc
        plugins.append(_instantiate_plugin(target))

    return PluginManager(plugins)


def _instantiate_plugin(target: Any) -> Any:
    return target() if callable(target) else target
