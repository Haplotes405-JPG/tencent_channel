"""凭证生命周期：开机未登录时打印登录二维码；关机（ON_STOP）时退出 CLI 登录。

两个开关都在 ``[cli.login]`` 配置节，**默认关闭**（保持「插件不代跑登录、不主动清凭证」的
原有行为）：

- ``cli.login.auto_qrcode``：适配器启动发现未登录时，打印授权链接与二维码（字符画 +
  PNG 路径），并后台等待扫码，成功后自动继续启动轮询。
- ``cli.login.logout_on_shutdown``：Bot 优雅关机时执行 ``tencent-channel-cli login logout``
  清除本机凭证（密钥链 + .env + 二维码缓存）。

注意：``cli.login.logout_on_shutdown`` 只在**优雅关机**时生效（``ON_STOP`` 事件）。被强杀 /
断电 / 关机流程超时不会执行，此时凭证会留到下次启动（下次启动若已登录就不会再要求扫码）。
"""

from __future__ import annotations

from typing import Any

from src.core.components.base import BaseEventHandler
from src.core.components.types import EventType
from src.kernel.event import EventDecision
from src.kernel.logger import get_logger

from .capabilities import plugin_enabled, tool_visible
from .cli_service import build_cli_client, cfg_get

logger = get_logger("tencent_channel.lifecycle")

#: 「工具筛选前」事件名：核心 **1.3.0** 起才有 ``BEFORE_TOOL_FILTER``（1.2.x 的 EventType
#: 里没有这个成员，直接写 ``EventType.BEFORE_TOOL_FILTER`` 会在 **import 阶段**把整个插件拖挂）。
#: 因此这里按名字探测：取不到就退化为「不订阅」，改由工具自身在执行时拒绝。
_TOOL_FILTER_EVENT_NAMES: tuple[str, ...] = ("BEFORE_TOOL_FILTER",)


def _resolve_tool_filter_event() -> Any:
    """取当前核心提供的「工具筛选前」事件；没有则返回 ``None``。"""
    for name in _TOOL_FILTER_EVENT_NAMES:
        event = getattr(EventType, name, None)
        if event is not None:
            return event
    return None


#: 当前核心的「工具筛选前」事件（``None`` = 这个核心不支持）
TOOL_FILTER_EVENT: Any = _resolve_tool_filter_event()

if TOOL_FILTER_EVENT is None:
    logger.warning(
        f"当前核心没有 {'/'.join(_TOOL_FILTER_EVENT_NAMES)} 事件（< 1.3.0）："
        "[capabilities] 关闭的工具无法从提示词隐藏，改由工具自身拒绝执行"
    )


class CapabilityGateHandler(BaseEventHandler):
    """按 ``[capabilities]`` 开关，把关闭的工具从提示词里摘掉。

    Action 的开关由框架原生的 ``go_activate()`` 负责；Tool 没有这个钩子，
    但核心 **1.3.0+** 的 ``ToolManager.filter_tools`` 会在筛选前发布 ``BEFORE_TOOL_FILTER``
    事件，处理器可以直接改写 ``component_classes``，于是关掉的工具连描述都不会进提示词。

    核心 1.2.x 没有该事件（此类的 ``init_subscribe`` 会退化为空列表）：此时关闭的工具
    仍会出现在提示词里，但 ``execute`` 会直接拒绝 —— 见 ``tools.py`` 里的能力自检。
    """

    name: str = "capability_gate"
    description: str = "按插件配置隐藏被关闭的 tencent_channel 只读工具"
    weight: int = 0
    intercept_message: bool = False
    #: 核心不支持该事件时退化为空列表（不订阅），避免 import 阶段就崩
    init_subscribe: list[EventType | str] = (
        [TOOL_FILTER_EVENT] if TOOL_FILTER_EVENT is not None else []
    )
    timeout: int = 0

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """筛选 Tool 组件类列表。"""
        classes = params.get("component_classes")
        if not isinstance(classes, list):
            return EventDecision.SUCCESS, params
        config = getattr(getattr(self, "plugin", None), "config", None)

        kept: list[Any] = []
        hidden: list[str] = []
        for component in classes:
            signature = ""
            resolver = getattr(component, "get_signature", None)
            if callable(resolver):
                try:
                    signature = str(resolver() or "")
                except Exception:  # noqa: BLE001 - 签名取不到就当不是本插件的组件
                    signature = ""
            if not signature.startswith("tencent_channel:"):
                kept.append(component)
                continue
            tool_name = str(getattr(component, "tool_name", "") or "")
            if tool_name and not tool_visible(config, tool_name):
                hidden.append(tool_name)
                continue
            kept.append(component)

        if hidden:
            logger.info(f"按 [capabilities] 开关隐藏工具：{', '.join(sorted(set(hidden)))}")
            params = dict(params)
            params["component_classes"] = kept
        return EventDecision.SUCCESS, params


class ShutdownLogoutHandler(BaseEventHandler):
    """Bot 关机时退出 tencent-channel-cli 登录（可选）。"""

    name: str = "shutdown_logout"
    description: str = "Bot 优雅关机（ON_STOP）时按配置清除 tencent-channel-cli 登录凭证"
    weight: int = 0
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_STOP]
    #: CLI 调用（login logout）可能要几秒，避开订阅者默认 30s 超时保护的边界情况
    timeout: int = 0

    async def execute(
        self, event_name: str, params: dict[str, Any]
    ) -> tuple[EventDecision, dict[str, Any]]:
        """ON_STOP 时按配置执行 ``login logout``。"""
        config = getattr(getattr(self, "plugin", None), "config", None)
        if not plugin_enabled(config):
            return EventDecision.SUCCESS, params
        if not bool(cfg_get(config, "cli.login", "logout_on_shutdown", False)):
            return EventDecision.SUCCESS, params

        logger.info("Bot 正在关机：按 cli.login.logout_on_shutdown 退出腾讯频道 CLI 登录…")
        # 关机路径给短超时，避免拖过框架的关闭窗口
        client = build_cli_client(config, logger=logger, timeout=15.0)
        try:
            result = await client.login_logout()
        except Exception as exc:  # noqa: BLE001 - 关机清理失败不该影响退出
            logger.warning(f"退出 CLI 登录异常（忽略）：{exc}")
            return EventDecision.SUCCESS, params

        if result.ok:
            logger.info("已退出腾讯频道 CLI 登录（本机凭证已清除）")
        else:
            logger.warning(f"退出 CLI 登录失败（忽略）：{result.error}")
        return EventDecision.SUCCESS, params
