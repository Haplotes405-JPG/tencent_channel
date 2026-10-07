"""框架 API 兼容层。

规范要求优先从公开入口 ``src.app.plugin_system.base`` 导入基类与配置工具；
但本机运行的是打包版（``mofox-backend.exe``），其内部名字集合可能与源码版存在差异。
为避免「少一个名字就整套插件加载失败」，这里做一次带回退的导入：

1. 首选公开入口 ``src.app.plugin_system.base``（规范推荐）；
2. 回退到内部路径 ``src.core.components.base``（本机内置插件 coding_agent 即用该路径）。

``task_manager`` 同理；若框架接口不可用，会退化为 ``asyncio`` 并打印一次 warning
（规范要求不要直接 ``asyncio.create_task``，因此这里是显式降级而不是首选）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

_logger = logging.getLogger("tencent_channel.compat")

try:  # 首选：插件系统公开入口
    from src.app.plugin_system.base import (  # type: ignore[import-not-found]
        BaseAction,
        BaseAdapter,
        BaseConfig,
        BasePlugin,
        BaseService,
        BaseTool,
        Field,
        SectionBase,
        config_section,
        register_plugin,
    )

    IMPORT_SOURCE = "src.app.plugin_system.base"
except ImportError as _exc:  # pragma: no cover - 取决于运行环境
    _logger.warning("公开入口导入失败（%s），回退到内部路径 src.core.components.base", _exc)
    from src.core.components.base import (  # type: ignore[import-not-found,no-redef]
        BaseAction,
        BaseAdapter,
        BaseConfig,
        BasePlugin,
        BaseService,
        BaseTool,
    )
    from src.core.components.base.config import (  # type: ignore[import-not-found,no-redef]
        Field,
        SectionBase,
        config_section,
    )
    from src.core.components.loader import (
        register_plugin,  # type: ignore[import-not-found,no-redef]
    )

    IMPORT_SOURCE = "src.core.components.base"


class _FallbackTaskManager:
    """框架 ``task_manager`` 不可用时的兜底实现。"""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._warned = False

    def create_task(
        self,
        coro: Coroutine[Any, Any, Any],
        name: str | None = None,
        daemon: bool = False,
        **kwargs: Any,
    ) -> SimpleNamespace:
        """创建任务并返回带 ``task_id`` 的句柄。"""
        if not self._warned:
            _logger.warning(
                "框架 task_manager 不可用，已回退到 asyncio.create_task（任务：%s）", name
            )
            self._warned = True
        task = asyncio.create_task(coro, name=name) if name else asyncio.create_task(coro)
        task_id = f"fallback-{id(task)}"
        self._tasks[task_id] = task

        def _cleanup(_done: asyncio.Task[Any]) -> None:
            self._tasks.pop(task_id, None)

        task.add_done_callback(_cleanup)
        return SimpleNamespace(task_id=task_id, task=task, cancel=task.cancel)

    def cancel_task(self, task_id: str) -> bool:
        """取消任务。"""
        task = self._tasks.pop(task_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True


_FALLBACK_TASK_MANAGER = _FallbackTaskManager()

try:  # 首选：框架统一任务管理器
    from src.kernel.concurrency import (
        get_task_manager,  # type: ignore[import-not-found]
    )

    TASK_MANAGER_SOURCE = "src.kernel.concurrency"
except ImportError:  # pragma: no cover - 取决于运行环境
    get_task_manager = None  # type: ignore[assignment]
    TASK_MANAGER_SOURCE = "asyncio-fallback"


def resolve_task_manager() -> Any:
    """返回任务管理器（框架实现优先，失败降级）。"""
    if get_task_manager is None:
        return _FALLBACK_TASK_MANAGER
    try:
        return get_task_manager()
    except Exception as exc:  # noqa: BLE001 - 降级优先于崩溃
        _logger.warning("获取框架 task_manager 失败（%s），使用 asyncio 兜底", exc)
        return _FALLBACK_TASK_MANAGER


__all__ = [
    "IMPORT_SOURCE",
    "TASK_MANAGER_SOURCE",
    "BaseAction",
    "BaseAdapter",
    "BaseConfig",
    "BasePlugin",
    "BaseService",
    "BaseTool",
    "Field",
    "SectionBase",
    "config_section",
    "register_plugin",
    "resolve_task_manager",
]
