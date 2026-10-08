"""Tool / Action 共享的助手（配置读取、CLI 客户端获取、结果渲染）。

抽出来避免 Tool 与 Action 各写一遍；不依赖框架基类，只依赖 ``cli_client`` /
``cli_service`` 与本插件的配置形状。
"""

from __future__ import annotations

import json
from typing import Any

from src.app.plugin_system.api import service_api
from src.kernel.logger import get_logger

from .cli_client import CliResult, TencentChannelCli
from .cli_service import SERVICE_SIGNATURE, build_cli_client, cfg_get
from .guild_resolver import (
    configured_guild_refs,
    iter_channel_entries,
    looks_like_channel_id,
    looks_like_guild_id,
    peek_cached,
    resolve_guild,
    resolve_section,
)

logger = get_logger("tencent_channel.component")

#: 渲染文本长度上限，避免巨型 JSON 灌进 LLM 上下文
MAX_RENDER_CHARS = 2000


def self_name_candidates(config: Any) -> list[str]:
    """配置里能代表「机器人昵称」的候选（判断「本条正文是否点名了机器人」时用）。

    适配器会把探测到的昵称/频道昵称一起并进来（见 ``TencentChannelAdapter._nickname_candidates``）。
    """
    names: list[str] = []
    for value in (cfg_get(config, "channel", "bot_name", ""),):
        text = str(value or "").strip()
        if text and text not in names:
            names.append(text)
    return names


async def resolve_section_target(
    cli: Any,
    config: Any,
    guild_id: str,
    explicit: str = "",
    *,
    allow_empty: bool = False,
) -> tuple[str, str]:
    """解析「发帖/评论用版块」，返回 ``(channel_id, 错误信息)``。

    版块 ID 在 QQ 客户端里看不到，所以三种写法都支持：

    - **版块 ID**（纯数字）→ 直接用，不查接口；
    - **版块名**（如「全部」「闲聊」）→ ``manage get-guild-channel-list`` 按名字换算；
    - **留空** → 配置 ``channel.channel_id`` → 配置 ``channel.section_name`` → 自动取「全部」版块，
      再退到第一个版块（都拿不到才报错）。

    Args:
        cli: ``TencentChannelCli`` / ``GatewayClient``（鸭子类型）。
        config: 插件配置对象。
        guild_id: 目标频道 ID（按名字解析时需要）。
        explicit: 调用方显式传入的版块 ID 或版块名。
        allow_empty: 为 True 时「解析不出来」不算错误（用于 channel_id 本身可选的能力）。

    为什么放在模块级：**Tool/Action（经 mixin）与适配器共用同一实现** —— 适配器不继承
    ``ChannelCliMixin``，两边各写一套必然漂移（本次就是靠验收测试才发现适配器里调不到 mixin 的方法）。
    """
    candidate = str(explicit or "").strip()
    if looks_like_channel_id(candidate):
        return candidate, ""

    configured_id = str(cfg_get(config, "channel", "channel_id", ""))
    configured_name = str(cfg_get(config, "channel", "section_name", ""))

    if getattr(cli, "dry_run", False) and not looks_like_channel_id(candidate):
        # dry_run 下接口不返回真实数据，名字没法换算：原样直通，让演练能看到「将要发送什么」
        passthrough = candidate or configured_id or configured_name
        logger.debug(f"dry_run 模式：跳过版块名解析，原样使用 {passthrough!r}")
        return (passthrough, "") if passthrough or not allow_empty else ("", "")

    ref = candidate or configured_id or configured_name
    if ref:
        resolution = await resolve_section(cli, guild_id=guild_id, ref=ref, logger=logger)
        if resolution.ok:
            return resolution.channel_id, ""
        return ("", "") if allow_empty else ("", resolution.message)

    if not guild_id:
        if allow_empty:
            return "", ""
        return "", (
            "缺少版块：请传 channel_id（版块名或版块 ID），或在插件配置 channel.section_name 填版块名"
            "（也可用 channel.channel_id 填版块 ID）。"
        )

    result = await cli.get_channel_list(guild_id)
    if not result.ok:
        if allow_empty:
            return "", ""
        return "", f"获取版块列表失败：{result.error}（{result.hint()}）"
    entries = iter_channel_entries(result.data if result.data is not None else result.raw)
    if not entries:
        if allow_empty:
            return "", ""
        return "", "该频道没有返回任何版块：请在 channel.section_name 指定默认版块"
    preferred = ("全部", "默认", "all", "default")
    picked = next(
        (item for item in entries if str(item.get("channel_name") or "").strip().lower() in preferred),
        entries[0],
    )
    return str(picked.get("channel_id") or ""), ""


class ChannelCliMixin:
    """为 Tool / Action 提供统一的配置访问与 CLI 调用入口。"""

    # ── 配置 ──────────────────────────────────────────────

    def config(self) -> Any:
        """本插件配置实例（拿不到时为 None，字段访问会落到默认值）。"""
        plugin = getattr(self, "plugin", None)
        return getattr(plugin, "config", None)

    def default_guild_id(self) -> str:
        """默认频道：配置里的**第一个**（已解析出真实 ID 就返回它，否则返回配置原值）。

        同步访问器（日志/展示/兜底）用这个；需要保证拿到真实 ID 的异步路径请用
        :meth:`target_guild`。
        """
        refs = configured_guild_refs(self.config())
        raw = refs[0] if refs else ""
        if not raw:
            return ""
        if looks_like_guild_id(raw):
            return raw
        cached = peek_cached(raw)
        return cached.guild_id if cached and cached.ok else raw

    def configured_guild_ids(self) -> list[str]:
        """配置里所有待监听的频道引用（``channel.guilds``）。"""
        return configured_guild_refs(self.config())

    async def target_guild(self, explicit: str = "", *, allow_empty: bool = False) -> tuple[str, str]:
        """解析目标频道，返回 ``(guild_id, 错误信息)``。

        三种输入都支持：真实 guild_id（纯数字）、频道号（``pd20589127``）、空（用
        ``channel.guilds`` 里的第一个）。成功时错误信息为空；失败时 ``guild_id`` 为空，
        错误信息可直接回给 LLM/用户。

        Args:
            explicit: 调用方显式传入的频道 ID 或频道号。
            allow_empty: 为 True 时「配置也为空」不算错误（CLI 允许省略 guild_id 的调用用）。
        """
        candidate = str(explicit or "").strip()
        if looks_like_guild_id(candidate):
            return candidate, ""
        if not candidate:
            refs = configured_guild_refs(self.config())
            if not refs:
                if allow_empty:
                    return "", ""
                return "", (
                    "缺少频道：请在插件配置 channel.guilds 里填监听频道（频道号形如 pd20589127，"
                    "插件会自动解析成真实 ID；也可直接填纯数字 ID），或调用时显式传入 guild_id。"
                )
            candidate = refs[0]

        resolution = await resolve_guild(self.cli(), guild_id=candidate, logger=logger)
        if resolution.ok:
            return resolution.guild_id, ""
        return "", resolution.message

    def default_channel_id(self) -> str:
        """配置中的默认版块 ID。"""
        return str(cfg_get(self.config(), "channel", "channel_id", ""))

    def self_tiny_id(self) -> str:
        """配置中的本账号 tiny_id（do-reply 的 replier_id）。"""
        return str(cfg_get(self.config(), "channel", "self_tiny_id", ""))

    def dm_source_guild_id(self) -> str:
        """私信发送使用的来源频道 ID。"""
        return str(cfg_get(self.config(), "channel", "dm_source_guild_id", "")) or self.default_guild_id()

    def configured_section_name(self) -> str:
        """配置里的默认版块「名字」（如「全部」「闲聊」）—— 版块 ID 在 QQ 里看不到，所以允许按名字配。"""
        return str(cfg_get(self.config(), "channel", "section_name", ""))

    def self_names(self) -> list[str]:
        """代表「机器人自己」的昵称候选（用于判断本条正文是否点名了它）。"""
        return self_name_candidates(self.config())

    async def resolve_section_id(
        self, guild_id: str, explicit: str = "", *, allow_empty: bool = False
    ) -> tuple[str, str]:
        """解析「发帖/评论用版块」（Tool / Action 用；适配器直接调 :func:`resolve_section_target`）。"""
        return await resolve_section_target(
            self.cli(), self.config(), guild_id, explicit, allow_empty=allow_empty
        )

    def reply_max_length(self) -> int:
        """回复正文长度上限。"""
        try:
            return int(cfg_get(self.config(), "reply", "max_length", 1000))
        except (TypeError, ValueError):
            return 1000

    def reply_to_comment_enabled(self) -> bool:
        """是否优先使用楼中楼回复。"""
        return bool(cfg_get(self.config(), "reply", "reply_to_comment", True))

    def enrich_comment_context_enabled(self) -> bool:
        """是否在 do-reply 前补齐必填字段。"""
        return bool(cfg_get(self.config(), "reply", "enrich_comment_context", True))

    # ── CLI ───────────────────────────────────────────────

    def cli(self) -> TencentChannelCli:
        """优先通过 Service 获取客户端；失败则按本地配置自建。"""
        try:
            return service_api.get_service(SERVICE_SIGNATURE).client()
        except Exception as exc:  # noqa: BLE001 - 降级优先于组件不可用
            logger.debug(f"获取 channel_cli 服务失败，改为本地构造客户端：{exc}")
            return build_cli_client(self.config(), logger=logger)

    # ── 结果渲染 ──────────────────────────────────────────

    @staticmethod
    def fail(result: CliResult) -> tuple[bool, str]:
        """把失败结果渲染成 (False, 文本+建议)。"""
        return False, f"调用失败：{result.error}\n处理建议：{result.hint()}"

    @staticmethod
    def render(payload: Any) -> str:
        """把任意返回渲染成有界文本。"""
        if isinstance(payload, str):
            text = payload
        else:
            try:
                text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            except (TypeError, ValueError):
                text = str(payload)
        if len(text) > MAX_RENDER_CHARS:
            text = text[:MAX_RENDER_CHARS] + "…（已截断）"
        return text
