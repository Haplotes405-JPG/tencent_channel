"""CLI 服务组件：把 ``tencent-channel-cli`` 封装成跨组件可复用的 Service。

Service 在框架里**不是单例**（每次 ``get_service`` 都会新建实例），因此这里不缓存
跨调用状态，只按需按配置构造一个轻量客户端（不启动子进程）。
"""

from __future__ import annotations

from typing import Any

from src.kernel.logger import get_logger

from ._compat import BaseService
from .cli_client import CliResult, TencentChannelCli
from .gateway_client import DEFAULT_GATEWAY_ENDPOINT, GatewayClient
from .guild_resolver import GuildResolution, resolve_guild

logger = get_logger("tencent_channel.service")

#: 组件签名（与 manifest.include 中的 component_name 对应）
SERVICE_SIGNATURE = "tencent_channel:service:channel_cli"


def cfg_get(config: Any, section: str, field: str, default: Any) -> Any:
    """从插件配置对象里安全取值（配置缺失/字段缺失时返回默认值）。

    ``section`` 支持**点号路径**，用于嵌套配置节：``cfg_get(config, "channel.notices",
    "poll_interval", 20.0)`` 等价于 ``config.channel.notices.poll_interval``。
    """
    node: Any = config
    for part in str(section).split("."):
        node = getattr(node, part, None) if node is not None else None
        if node is None:
            return default
    value = getattr(node, field, default)
    return default if value is None else value


def build_cli_client(
    config: Any = None, logger: Any = None, *, timeout: float | None = None
) -> TencentChannelCli:
    """按配置构造 CLI 客户端（纯构造，不执行任何命令）。

    Args:
        timeout: 覆盖配置里的 ``cli.timeout``；退登等关路径调用会传更短的值，
            避免结算阶段卡住。

    ``cli.mode = "gateway"`` 时返回 :class:`GatewayClient`（免 CLI 直连 MCP 网关），
    令牌来源与 token 直登一致：配置的 cli.login.token / cli.login.token_file 优先，
    留空则读本机凭据存储里 CLI 登录过的令牌。
    """
    mode = str(cfg_get(config, "cli", "mode", "auto")).strip().lower()
    common = dict(
        timeout=float(timeout if timeout is not None else cfg_get(config, "cli", "timeout", 60.0)),
        dry_run=bool(cfg_get(config, "cli", "dry_run", False)),
        rate_limit_sleep=float(cfg_get(config, "cli", "rate_limit_sleep", 70.0)),
        rate_limit_multiplier=float(cfg_get(config, "cli", "rate_limit_multiplier", 2.0)),
        rate_limit_ceiling=float(cfg_get(config, "cli", "rate_limit_ceiling", 1800.0)),
        logger=logger,
    )
    if mode == "gateway":
        from .token_login import TokenLoginError, resolve_login_credential

        credential = None
        try:
            credential = resolve_login_credential(config)
        except TokenLoginError as exc:
            if logger is not None:
                logger.warning(f"gateway 令牌配置无效（退回读凭据存储）：{exc}")
        if credential is None:
            from .token_login import read_stored_login

            token, _device = read_stored_login()
            if token and logger is not None:
                logger.warning(
                    "gateway 模式没有配置令牌，已改用本机凭据存储里的令牌 —— 它是**全机器共享**的，"
                    "同机多实例时可能是别的实例（别的 bot）的账号；请在 [cli.login] 配 token_file"
                )
        else:
            token = credential.token
        return GatewayClient(
            endpoint=str(cfg_get(config, "cli", "gateway_endpoint", DEFAULT_GATEWAY_ENDPOINT) or DEFAULT_GATEWAY_ENDPOINT),
            token=token,
            **common,
        )
    return TencentChannelCli(
        path=str(cfg_get(config, "cli", "path", "tencent-channel-cli.cmd")),
        mode=mode,
        timeout=common["timeout"],
        dry_run=common["dry_run"],
        node_path=str(cfg_get(config, "cli", "node_path", "") or ""),
        python_path=str(cfg_get(config, "cli", "python_path", "") or ""),
        rate_limit_sleep=common["rate_limit_sleep"],
        rate_limit_multiplier=common["rate_limit_multiplier"],
        rate_limit_ceiling=common["rate_limit_ceiling"],
        logger=logger,
    )


class ChannelCLIService(BaseService):
    """腾讯频道 CLI 调用服务。

    提供只读查询与（非破坏性）写操作；所有方法返回 :class:`CliResult`，
    调用方按 ``result.kind`` 决定提示语，不抛业务异常。
    """

    #: 组件名与描述用当前的 ``name`` / ``description``（``service_name`` / ``service_description``
    #: 是核心的 legacy 别名，工具链会告警）。实测 1.2.0 与 1.3.0-alpha 都以 name/description 为准。
    name = "channel_cli"
    description = "腾讯频道 CLI（tencent-channel-cli）调用封装：通知、帖子、评论、私信、频道信息"

    # ── 客户端 ──────────────────────────────────────────────

    def client(self) -> TencentChannelCli:
        """按当前插件配置构造客户端（Service 非单例，故每次新建）。"""
        plugin = getattr(self, "plugin", None)
        return build_cli_client(getattr(plugin, "config", None), logger=logger)

    # ── 只读 ────────────────────────────────────────────────

    async def version(self) -> CliResult:
        """查询 CLI 版本。"""
        return await self.client().version()

    async def login_status(self) -> CliResult:
        """查询登录状态。"""
        return await self.client().login_status()

    async def get_notices(
        self, *, guild_id: str = "", page_num: int | None = None, attach_info: str = ""
    ) -> CliResult:
        """互动消息（评论/回复/@/点赞），纯读取。"""
        return await self.client().get_notices(guild_id=guild_id, page_num=page_num, attach_info=attach_info)

    async def get_feed_detail(self, feed_id: str, *, guild_id: str = "", channel_id: str = "") -> CliResult:
        """帖子详情。"""
        return await self.client().get_feed_detail(feed_id, guild_id=guild_id, channel_id=channel_id)

    async def get_feed_comments(
        self,
        feed_id: str,
        *,
        guild_id: str = "",
        channel_id: str = "",
        count: int = 20,
        attach_info: str = "",
    ) -> CliResult:
        """帖子评论列表。"""
        return await self.client().get_feed_comments(
            feed_id, guild_id=guild_id, channel_id=channel_id, count=count, attach_info=attach_info
        )

    async def search_feeds(self, query: str, *, guild_id: str, next_page_cookie: str = "") -> CliResult:
        """频道内搜索帖子。"""
        return await self.client().search_feeds(query, guild_id=guild_id, next_page_cookie=next_page_cookie)

    async def get_guild_info(self, guild_id: str) -> CliResult:
        """频道基本信息。"""
        return await self.client().get_guild_info(guild_id)

    async def get_channel_list(self, guild_id: str) -> CliResult:
        """频道版块列表。"""
        return await self.client().get_channel_list(guild_id)

    async def get_my_guilds(self) -> CliResult:
        """我加入/创建/管理的频道列表（用于获取真实 guild_id）。"""
        return await self.client().get_my_guilds()

    async def get_guild_feeds(
        self, *, guild_id: str, get_type: int = 2, count: int = 10, feed_attach_info: str = ""
    ) -> CliResult:
        """频道主页帖子列表（1=热门 2=最新），用于发现新帖子。"""
        return await self.client().get_guild_feeds(
            guild_id=guild_id, get_type=get_type, count=count, feed_attach_info=feed_attach_info
        )

    async def search_guild_content(
        self, keyword: str, *, scope: str = "channel", next_page_token: str = ""
    ) -> CliResult:
        """搜索腾讯频道/帖子/作者（``scope=channel`` 可用于频道号 → guild_id）。"""
        return await self.client().search_guild_content(
            keyword, scope=scope, next_page_token=next_page_token
        )

    async def resolve_guild_id(self, guild_id: str = "", guild_number: str = "") -> GuildResolution:
        """把频道号（或真实 ID）解析成可用 ``guild_id``；解析结果进程内缓存。"""
        return await resolve_guild(
            self.client(), guild_id=guild_id, guild_number=guild_number, logger=logger
        )

    async def search_members(self, keyword: str, *, guild_id: str, num: int = 20) -> CliResult:
        """搜索频道成员（获取 tiny_id）。"""
        return await self.client().search_members(keyword, guild_id=guild_id, num=num)

    async def get_user_info(self, **params: Any) -> CliResult:
        """查看用户资料。"""
        return await self.client().get_user_info(**params)

    # ── 写（全部非破坏性；comment_type / reply_type 固定为 1，永不传 --yes）──

    async def publish_feed(
        self,
        content: str,
        *,
        guild_id: str = "",
        channel_id: str = "",
        title: str = "",
        at_users: list[dict[str, str]] | None = None,
        links: list[dict[str, str]] | None = None,
    ) -> CliResult:
        """发帖。"""
        return await self.client().publish_feed(
            content, guild_id=guild_id, channel_id=channel_id, title=title, at_users=at_users, links=links
        )

    async def do_comment(
        self,
        content: str,
        *,
        feed_id: str,
        feed_create_time: Any = "",
        guild_id: str = "",
        channel_id: str = "",
        at_users: list[dict[str, str]] | None = None,
    ) -> CliResult:
        """评论帖子。"""
        return await self.client().do_comment(
            content,
            feed_id=feed_id,
            feed_create_time=feed_create_time,
            guild_id=guild_id,
            channel_id=channel_id,
            at_users=at_users,
        )

    async def do_reply(
        self,
        content: str,
        *,
        feed_id: str,
        comment_id: str,
        extra_fields: dict[str, Any] | None = None,
        guild_id: str = "",
        channel_id: str = "",
    ) -> CliResult:
        """楼中楼回复评论。"""
        return await self.client().do_reply(
            content,
            feed_id=feed_id,
            comment_id=comment_id,
            extra_fields=extra_fields,
            guild_id=guild_id,
            channel_id=channel_id,
        )

    async def push_dm(
        self,
        text: str,
        *,
        peer_tiny_id: str = "",
        source_guild_id: str = "",
        ref: int | None = None,
    ) -> CliResult:
        """私信（主动发送或按通知编号回复）。"""
        return await self.client().push_dm(
            text, peer_tiny_id=peer_tiny_id, source_guild_id=source_guild_id, ref=ref
        )
