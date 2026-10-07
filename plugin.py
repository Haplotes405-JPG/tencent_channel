"""腾讯频道（QQ 频道）插件入口。

组件清单（必须与 ``manifest.json`` 的 ``include`` 保持一致）：

- config:   ``config``
- adapter:  ``tencent_channel_adapter``
- service:  ``channel_cli``
- tool:     ``channel_notices`` / ``channel_search_feeds`` / ``channel_feed_detail`` /
            ``channel_guild_info`` / ``channel_members`` / ``channel_guilds``（本账号所在频道列表）/
            ``channel_sections``（频道内的版块列表）/ ``channel_read``（通用只读）
- action:   ``channel_publish_feed`` / ``channel_comment_feed`` /
            ``channel_reply_comment`` / ``channel_like`` / ``channel_send_dm`` /
            ``channel_write``（通用写入，覆盖 CLI 其余写命令）
- event_handler: ``shutdown_logout``（关机退登）、``capability_gate``（按开关隐藏工具；
  核心 < 1.3.0 没有 ``BEFORE_TOOL_FILTER`` 事件，此时退化为「工具自身拒绝执行」，见 ``lifecycle.py``）

开关与提示词：``[capabilities]`` 逐能力开关、``[prompts]`` 全部提示词，
能力清单由 ``gen_capability_spec.py`` 从 CLI schema 生成到 ``capability_spec.py``。
"""

from __future__ import annotations

from src.kernel.logger import get_logger

from ._compat import BasePlugin, register_plugin
from .actions import ACTIONS
from .adapter import TencentChannelAdapter
from .capabilities import (
    EXCLUDED,
    apply_prompt_overrides,
    describe_excluded,
    enabled_capabilities,
    exposure,
    plugin_enabled,
)
from .cli_service import ChannelCLIService
from .config import TencentChannelConfig
from .guild_resolver import configured_guild_refs
from .lifecycle import CapabilityGateHandler, ShutdownLogoutHandler
from .tools import TOOLS

logger = get_logger("tencent_channel.plugin")


@register_plugin
class TencentChannelPlugin(BasePlugin):
    """腾讯频道（QQ 频道）插件。"""

    plugin_name = "tencent_channel"
    plugin_description = "通过 tencent-channel-cli 接入腾讯频道（QQ 频道）：通知轮询、自动回复、发帖与频道查询"
    plugin_version = "1.0.3"

    #: 配置类必须放在 configs 里（真实约束），不要放进 get_components()
    #: 注意：写普通赋值（不带类型注解）—— 工具链的 CodeParser 只认 Assign，带注解会被判成「未定义配置类」
    configs = [TencentChannelConfig]
    dependent_components: list[str] = []

    def get_components(self) -> list[type]:
        """返回组件类（返回类本身，不是实例）。"""
        return [
            TencentChannelAdapter,
            ChannelCLIService,
            ShutdownLogoutHandler,
            CapabilityGateHandler,
            *TOOLS,
            *ACTIONS,
        ]

    async def on_plugin_loaded(self) -> None:
        """插件加载完成钩子：按配置刷新提示词，并汇报能力开关概况。"""
        config = getattr(self, "config", None)
        applied = apply_prompt_overrides(config)
        reads = enabled_capabilities(config, ("read",))
        writes = enabled_capabilities(config, ("write",))
        sensitive = enabled_capabilities(config, ("sensitive",))
        danger = enabled_capabilities(config, ("danger",))
        generic_read, generic_write = exposure(config)
        if not plugin_enabled(config):
            logger.warning(
                "腾讯频道插件已加载，但 [plugin] enabled=false：整体停用（不轮询、不登录、"
                "工具与动作都不进提示词）"
            )
            return
        logger.info(
            f"腾讯频道插件已加载：适配器 1 个、CLI 服务 1 个、工具 {len(TOOLS)} 个、动作 {len(ACTIONS)} 个"
            f"；提示词已刷新 {len(applied)} 个；能力开关：只读 {len(reads)}、写入 {len(writes)}、"
            f"敏感 {len(sensitive)}、破坏性 {len(danger)}"
            f"（通用工具 {generic_read}/{generic_write}）；监听频道 "
            f"{len(configured_guild_refs(config))} 个"
        )
        logger.debug(
            f"被排除的 CLI 命令（{len(EXCLUDED)} 条）：\n{describe_excluded()}"
        )
