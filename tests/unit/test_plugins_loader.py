from __future__ import annotations

import pytest

from tgshelf.config import PluginsConfig
from tgshelf.plugins import PluginContext, PluginHost, PluginNode
from tgshelf.plugins.loader import load_plugins


@pytest.mark.asyncio
async def test_load_plugins_imports_configured_plugin_from_extra_path(tmp_path) -> None:
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "sample_plugin.py").write_text(
        """
class SamplePlugin:
    def __init__(self):
        self.calls = []

    async def after_file_upload(self, ctx):
        self.calls.append((ctx.operation, ctx.node.id))
"""
    )

    manager = load_plugins(
        PluginsConfig(
            enabled=True,
            paths=(str(plugin_dir),),
            modules=("sample_plugin:SamplePlugin",),
        )
    )
    ctx = PluginContext(
        operation="file_upload",
        host=PluginHost(object()),
        node=PluginNode(
            id="file",
            name="Movie.mkv",
            parent_id="parent",
            is_folder=False,
            mime="video/x-matroska",
            size=5,
            state="ACTIVE",
            info={},
        ),
    )

    await manager.run_after("after_file_upload", ctx)

    assert manager.enabled is True
    assert manager._plugins[0].calls == [("file_upload", "file")]


def test_load_plugins_disabled_ignores_missing_modules() -> None:
    manager = load_plugins(
        PluginsConfig(
            enabled=False,
            modules=("missing_module:MissingPlugin",),
        )
    )

    assert manager.enabled is False
