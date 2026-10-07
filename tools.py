"""LLM 可调用的只读工具（Tool）。

按规范：Tool 只负责查询与返回信息，不承担副作用；写操作在 ``actions.py``。

为什么每个工具都要 ``self._gate()``：核心 **1.2.x** 没有 ``BEFORE_TOOL_FILTER`` 事件，
``capability_gate`` 处理器订阅不到（见 ``lifecycle.py``），关掉的能力无法从提示词里隐藏；
所以工具自身在执行时再校验一次能力开关，保证「配置关掉 = 一定不执行」。
"""

from __future__ import annotations

from typing import Annotated

from ._compat import BaseTool
from ._component import ChannelCliMixin
from .capabilities import (
    KIND_READ,
    READ_TOOL_CAPABILITY,
    capability_by_name,
    capability_enabled,
    enabled_capabilities,
    plugin_enabled,
)
from .cli_client import normalize_params
from .guild_resolver import iter_channel_entries, iter_guild_entries
from .notice_mapping import extract_notices, normalize_notice, notice_text


class _ChannelTool(ChannelCliMixin, BaseTool):
    """本插件所有 Tool 的公共基类。

    核心 1.3.0+ 会在筛选前用 ``BEFORE_TOOL_FILTER`` 把关掉的工具从提示词里摘掉；
    核心 1.2.x 没有这个事件，所以这里再做一次**能力自检**兜底（拒绝执行 + 说明原因）。
    """

    def _gate(self) -> str:
        """能力被配置关闭时返回拒绝理由（空串 = 放行）。"""
        config = self.config()
        if not plugin_enabled(config):
            return "腾讯频道插件已在 [plugin] enabled=false 里整体停用。"
        capability = READ_TOOL_CAPABILITY.get(str(getattr(self, "tool_name", "")))
        if capability and not capability_enabled(config, capability):
            return (
                f"{getattr(self, 'tool_name', '该工具')} 已被配置关闭"
                "（[capabilities] disabled，或该类别默认值为 false）。"
            )
        return ""


class ChannelNoticesTool(_ChannelTool):
    """查看腾讯频道最近的互动通知。"""

    tool_name = "channel_notices"
    tool_description = (
        "查看腾讯频道最近的互动通知（收到评论、收到回复、被@）。"
        "返回通知类型、评论者、内容、帖子 ID 与评论 ID，可用于后续评论或回复。"
    )

    async def execute(
        self,
        limit: Annotated[int, "最多返回多少条通知，默认 10"] = 10,
        guild_id: Annotated[str, "腾讯频道 ID，留空则使用插件配置里的默认频道"] = "",
    ) -> tuple[bool, str]:
        """查询互动通知列表。"""
        if (reason := self._gate()):
            return False, reason
        target, error = await self.target_guild(guild_id)
        if error:
            return False, error
        result = await self.cli().get_notices(guild_id=target, page_num=max(1, min(limit, 50)))
        if not result.ok:
            return self.fail(result)

        notices = [
            normalize_notice(item, self_names=self.self_names()) for item in extract_notices(result.data)
        ]
        if not notices:
            return True, "当前没有可读取的互动通知。"

        lines: list[str] = []
        for notice in notices[: max(1, limit)]:
            lines.append(notice_text(notice))
            ids = [f"帖子ID={notice['feed_id']}"] if notice["feed_id"] else []
            if notice["comment_id"]:
                ids.append(f"评论ID={notice['comment_id']}")
            if notice["guild_id"]:
                ids.append(f"频道ID={notice['guild_id']}")
            if ids:
                lines.append("  " + "，".join(ids))
        lines.append("提示：已自动注入当前会话的通知不用重复拉取（本工具只是补看历史）；要不要回复由你自己判断。")
        return True, "\n".join(lines)


class ChannelSearchFeedsTool(_ChannelTool):
    """在指定腾讯频道内搜索帖子。"""

    tool_name = "channel_search_feeds"
    tool_description = "在当前腾讯频道内按关键词搜索帖子，返回帖子 ID、标题与摘要。需要账号已加入该频道。"

    async def execute(
        self,
        query: Annotated[str, "搜索关键词"],
        guild_id: Annotated[str, "腾讯频道 ID，留空则使用插件配置里的默认频道"] = "",
    ) -> tuple[bool, str]:
        """搜索帖子。"""
        if (reason := self._gate()):
            return False, reason
        target, error = await self.target_guild(guild_id)
        if error:
            return False, error
        result = await self.cli().search_feeds(query, guild_id=target)
        if not result.ok:
            return self.fail(result)
        return True, self.render(result.data)


class ChannelFeedDetailTool(_ChannelTool):
    """查看单条帖子详情。"""

    tool_name = "channel_feed_detail"
    tool_description = "查看指定帖子的详情（作者、时间、正文、分享链接），返回内容可用于评论或回复。"

    async def execute(
        self,
        feed_id: Annotated[str, "帖子 ID（形如 B_xxx）"],
        guild_id: Annotated[str, "腾讯频道 ID，留空则使用插件配置里的默认频道"] = "",
    ) -> tuple[bool, str]:
        """查询帖子详情。"""
        if (reason := self._gate()):
            return False, reason
        target, error = await self.target_guild(guild_id, allow_empty=True)
        if error:
            return False, error
        result = await self.cli().get_feed_detail(feed_id, guild_id=target)
        if not result.ok:
            return self.fail(result)
        return True, self.render(result.data)


class ChannelGuildInfoTool(_ChannelTool):
    """查看腾讯频道基本信息。"""

    tool_name = "channel_guild_info"
    tool_description = "查看腾讯频道基本信息（名称、成员数、公告、加入设置等）。"

    async def execute(
        self,
        guild_id: Annotated[str, "腾讯频道 ID，留空则使用插件配置里的默认频道"] = "",
    ) -> tuple[bool, str]:
        """查询频道信息。"""
        if (reason := self._gate()):
            return False, reason
        target, error = await self.target_guild(guild_id)
        if error:
            return False, error
        result = await self.cli().get_guild_info(target)
        if not result.ok:
            return self.fail(result)
        return True, self.render(result.data)


class ChannelMembersTool(_ChannelTool):
    """搜索腾讯频道成员。"""

    tool_name = "channel_members"
    tool_description = (
        "按昵称搜索腾讯频道成员，返回成员的 tiny_id（内部用户 ID）。"
        "@某人时必须先用本工具拿到 tiny_id，严禁使用 QQ 号或猜测值。"
    )

    async def execute(
        self,
        keyword: Annotated[str, "成员昵称关键词"],
        guild_id: Annotated[str, "腾讯频道 ID，留空则使用插件配置里的默认频道"] = "",
        num: Annotated[int, "最多返回多少个成员，默认 20"] = 20,
    ) -> tuple[bool, str]:
        """搜索成员。"""
        if (reason := self._gate()):
            return False, reason
        target, error = await self.target_guild(guild_id)
        if error:
            return False, error
        result = await self.cli().search_members(keyword, guild_id=target, num=max(1, min(num, 50)))
        if not result.ok:
            return self.fail(result)
        return True, self.render(result.data)


class ChannelGuildsTool(_ChannelTool):
    """查看本账号加入了哪些腾讯频道。"""

    tool_name = "channel_guilds"
    tool_description = (
        "查看本账号加入/创建/管理的腾讯频道列表，返回频道名称、频道号（pd…）与真实频道 ID。"
        "多频道场景下用它确认该操作哪个频道；返回的频道号或频道 ID 都能当其它工具的 guild_id 用。"
    )

    async def execute(self) -> tuple[bool, str]:
        """查询「我的腾讯频道」列表。"""
        if (reason := self._gate()):
            return False, reason
        result = await self.cli().get_my_guilds()
        if not result.ok:
            return self.fail(result)
        entries = iter_guild_entries(result.data if result.data is not None else result.raw)
        if not entries:
            return True, "接口没有返回任何频道（账号可能还没加入任何腾讯频道）。"
        lines = ["本账号所在频道："]
        for entry in entries[:20]:
            parts = [f"名称={entry.guild_name or '（无名）'}"]
            if entry.guild_number:
                parts.append(f"频道号={entry.guild_number}")
            parts.append(f"频道ID={entry.guild_id}")
            lines.append("  " + "，".join(parts))
        lines.append("提示：把频道号或频道 ID 传给其它工具的 guild_id 参数即可指定频道。")
        return True, "\n".join(lines)


class ChannelSectionsTool(_ChannelTool):
    """查看腾讯频道内的版块（板块）列表。"""

    tool_name = "channel_sections"
    tool_description = (
        "查看腾讯频道内的版块（板块）列表，返回版块名称与版块 ID。"
        "发帖要指定发到哪个版块时：把这里的版块名或版块 ID 传给 channel_publish_feed 的 channel_id 即可。"
    )

    async def execute(
        self,
        guild_id: Annotated[str, "腾讯频道 ID，留空则使用插件配置里的默认频道"] = "",
    ) -> tuple[bool, str]:
        """查询频道版块列表。"""
        if (reason := self._gate()):
            return False, reason
        target, error = await self.target_guild(guild_id)
        if error:
            return False, error
        result = await self.cli().get_channel_list(target)
        if not result.ok:
            return self.fail(result)
        entries = iter_channel_entries(result.data if result.data is not None else result.raw)
        if not entries:
            return True, "该频道没有返回任何版块。"
        lines = ["频道版块："]
        for item in entries[:20]:
            lines.append(
                f"  版块名={item.get('channel_name') or '（无名）'}，版块ID={item.get('channel_id')}"
            )
        lines.append(
            "提示：发帖时把版块名或版块 ID 传给 channel_publish_feed 的 channel_id；不传则用配置里的默认版块。"
        )
        return True, "\n".join(lines)


class ChannelReadTool(_ChannelTool):
    """通用只读工具：执行能力清单里的任意一条只读命令（清单与开关都在配置里）。"""

    tool_name = "channel_read"
    tool_description = "查询腾讯频道只读信息（可选命令见插件配置 [prompts] read_tool）。"

    async def execute(
        self,
        command: Annotated[str, "只读命令名，形如 'feed.get-feed-comments'；可选值见工具描述里的清单"],
        params: Annotated[dict, "该命令的参数（JSON 对象，键名见工具描述；带 * 的是必填）"] = {},
    ) -> tuple[bool, str]:
        """执行一条只读命令。"""
        config = self.config()
        if not plugin_enabled(config):
            return False, "腾讯频道插件已在 [plugin] enabled=false 里整体停用。"
        cap = capability_by_name(command)
        if cap is None:
            available = "、".join(c.name for c in enabled_capabilities(config, (KIND_READ,)))
            return False, f"未知命令 {command!r}。当前可用：{available}"
        if cap.kind != KIND_READ:
            return False, f"{cap.name} 属于写操作，请用 channel_write 或对应的专用动作。"
        if not capability_enabled(config, cap.name):
            return False, (
                f"命令 {cap.name} 已被配置关闭（[capabilities] disabled 或该类别默认值为 false）。"
            )

        payload = normalize_params(params or {})
        # 频道 / 版块缺省值：只在命令确实有这些参数时补
        if cap.domain != "cli":
            if "guild_id" in cap.params and not payload.get("guild_id"):
                target, error = await self.target_guild("", allow_empty=True)
                if error:
                    return False, error
                if target:
                    payload["guild_id"] = target
            if "channel_id" in cap.params and not payload.get("channel_id"):
                default_channel = self.default_channel_id()
                if default_channel:
                    payload["channel_id"] = default_channel

        missing = [name for name in cap.required if not payload.get(name)]
        if missing:
            return False, f"{cap.name} 缺少必填参数：{'、'.join(missing)}"

        result = await self.cli().run_command(
            cap.domain, cap.action, payload, plain_text=(cap.domain == "cli")
        )
        if not result.ok:
            return self.fail(result)
        return True, self.render(result.data)


#: 供 plugin.py 注册
TOOLS: list[type] = [
    ChannelNoticesTool,
    ChannelSearchFeedsTool,
    ChannelFeedDetailTool,
    ChannelGuildInfoTool,
    ChannelMembersTool,
    ChannelGuildsTool,
    ChannelSectionsTool,
    ChannelReadTool,
]
