from __future__ import annotations

import logging

import pytest

from tgshelf.plugins import PluginContext, PluginError, PluginManager


class RecordingPlugin:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    async def before_file_upload(self, ctx) -> None:
        self.calls.append(f"{self.name}:{ctx.operation}:{ctx.node.id}")

    async def after_file_upload(self, ctx) -> None:
        self.calls.append(f"{self.name}:{ctx.operation}:{ctx.node.id}")


class BlockingPlugin:
    async def before_file_upload(self, ctx) -> None:
        raise PluginError("blocked by policy")


class FailingAfterPlugin:
    async def after_file_upload(self, ctx) -> None:
        raise RuntimeError("metadata lookup failed")


@pytest.mark.asyncio
async def test_plugin_manager_runs_hooks_in_order() -> None:
    calls: list[str] = []
    manager = PluginManager([RecordingPlugin("one", calls), RecordingPlugin("two", calls)])
    ctx = PluginContext(operation="file_upload", host=None, node=type("Node", (), {"id": "file"})())

    await manager.run_before("before_file_upload", ctx)
    await manager.run_after("after_file_upload", ctx)

    assert calls == [
        "one:file_upload:file",
        "two:file_upload:file",
        "one:file_upload:file",
        "two:file_upload:file",
    ]


@pytest.mark.asyncio
async def test_plugin_manager_before_hook_blocks_with_plugin_error() -> None:
    manager = PluginManager([BlockingPlugin()])
    ctx = PluginContext(operation="file_upload", host=None, node=type("Node", (), {"id": "file"})())

    with pytest.raises(PluginError, match="blocked by policy"):
        await manager.run_before("before_file_upload", ctx)


@pytest.mark.asyncio
async def test_plugin_manager_after_hook_logs_and_continues(caplog) -> None:
    manager = PluginManager([FailingAfterPlugin()])
    ctx = PluginContext(operation="file_upload", host=None, node=type("Node", (), {"id": "file"})())
    caplog.set_level(logging.ERROR, logger="tgshelf.plugins")

    await manager.run_after("after_file_upload", ctx)

    assert "[plugin] FailingAfterPlugin.after_file_upload failed: metadata lookup failed" in caplog.text
