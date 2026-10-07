"""腾讯频道（QQ 频道）轮询适配器。

数据流::

    feed get-notices（轮询） → notice_mapping → MessageEnvelope(dict) → core_sink.send
      → receiver → distributor → 默认 Chatter(LLM)
      → MessageSender（按 platform 找适配器） → _send_platform_message
      → reply_sender → tencent-channel-cli feed do-comment / do-reply / manage push-group-dm-msg

已核实的框架行为（Neo-MoFox 源码）：

- ``from_platform_message`` / ``get_bot_info`` 是抽象方法，必须实现。
- 出站由 ``MessageSender`` 调用 ``adapter._send_platform_message(envelope)``，
  适配器按 ``adapter_cls.platform == message.platform`` 命中（首个匹配者）。
- 入站 envelope 必须 ``direction="incoming"`` 且带 ``message_info`` 与 ``message_segment``；
  群/私聊由 ``group_info`` 是否存在决定，``stream_id`` 由框架按
  ``sha256(f"{platform}_{group_id}")`` / ``sha256(f"{platform}_{user_id}_private")`` 推导。
- 无 transport 时基类 ``is_connected()`` 恒为 False，默认 ``health_check()`` 会因此每 30s
  触发一次 ``reconnect()``（stop+start）——因此这里重写 ``is_connected`` / ``health_check`` /
  ``reconnect``，reconnect 只重启轮询任务，不拆适配器本体。
- 出站 envelope **不含** ``raw_message`` / ``extra``，所以出站目标只能从
  ``message_info.group_info.group_id`` 或 ``message_info.user_info.user_id`` 反解，
  再查本适配器维护的上下文表（路由键同时编码进 group_id，重启后仍可反解出帖子/评论）。

安全边界：本适配器只发评论 / 回复 / 私信等非破坏性内容，``comment_type`` 与
``reply_type`` 固定为 1，永不传 ``--yes``；登录、开启通知（notices-on）等需要人工确认的
动作一律只提示、不代跑。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.kernel.logger import get_logger

from ._compat import BaseAdapter, resolve_task_manager
from ._component import resolve_section_target
from .cli_client import CliResult, TencentChannelCli
from .cli_service import build_cli_client, cfg_get
from .guild_resolver import (
    configured_guild_refs,
    iter_channel_entries,
    looks_like_guild_id,
    resolve_guild,
)
from .notice_mapping import (
    NoticeRoute,
    NoticeWatermark,
    canonical_notice_id,
    envelope_for_notice,
    extract_comments,
    extract_feeds,
    extract_notices,
    extract_replies,
    extract_target,
    extract_text,
    find_detail_author_id,
    find_detail_nickname,
    find_field,
    is_actionable,
    match_comment_by_body,
    normalize_notice,
    notice_from_comment,
    notice_from_feed,
    route_for_notice,
)
from .qr_ascii import describe_qr_source, render_qr_ascii
from .reply_sender import (
    find_comment_payload,
    send_comment_reply,
    send_dm_reply,
    truncate,
)
from .token_login import (
    LoginCredential,
    TokenLoginError,
    inject_login_credential,
    read_stored_login,
    resolve_login_credential,
)

logger = get_logger("tencent_channel.adapter")

#: 平台标识：必须全局唯一（出站按它匹配适配器）
PLATFORM = "tencent_channel"
ADAPTER_SIGNATURE = "tencent_channel:adapter:tencent_channel_adapter"
DEFAULT_NOTICE_TYPES = ("comment", "reply", "at", "dm")


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_text(value: Any) -> str:
    """取纯文本标量（None/bool/容器都算空）。"""
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _interval_text(seconds: float) -> str:
    """把间隔秒渲染成日志里的可读形式（整分钟就显示分钟，避免「间隔=600s」看不出是 10 分钟）。"""
    value = float(seconds or 0.0)
    if value >= 60 and abs(value % 60) < 0.5:
        return f"{value / 60:.0f}min（{value:.0f}s）"
    return f"{value:.0f}s"


#: 帖子详情缓存有效期（秒）；同一帖子的多条通知不必重复拉详情
_DETAIL_TTL_SECONDS = 120.0
#: 详情缓存条目上限
_DETAIL_CACHE_MAX = 50
#: 单个会话的待回复目标上限
_PENDING_TARGET_MAX = 30
#: 「跨路径同内容」去重窗口（秒）。
#: 互动通知不带 comment_id/reply_id，算出的键与评论轮询路径的键天然不同，
#: 同一条回复会被两条路径各注入一次（真机已证实）；这里用「帖子 + 正文」指纹兜底。
_CONTENT_DEDUP_TTL_SECONDS = 600.0
#: 指纹表上限
_CONTENT_DEDUP_MAX = 500

#: 通知摘要的前缀（``feed get-notices`` 的 summary 形如「回复了我:<正文>」），
#: 指纹比对时去掉，才能和评论轮询路径拿到的正文对齐。
_SUMMARY_PREFIXES = (
    "@了我:",
    "回复了我:",
    "评论了我:",
    "赞了我的回复:",
    "赞了我的评论:",
    "赞了我:",
    "顶了我的帖子:",
    "收藏了我的帖子:",
)


def _content_fingerprint(notice: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """跨路径同内容指纹：``(guild_id, feed_id, 规范化正文)``；正文太短/为空时返回 ``None``。

    指纹里必须带频道：多频道监听时不同频道完全可能发一模一样的内容，
    不带频道就会把别的频道的正常消息误判成重复。
    """
    content = _as_text(notice.get("content")).strip()
    if not content:
        return None
    for prefix in _SUMMARY_PREFIXES:
        if content.startswith(prefix):
            content = content[len(prefix) :].strip()
            break
    content = re.sub(r"^@\S+\s*", "", content)  # 新帖路径的正文可能以 @某人 开头
    content = " ".join(content.split()).casefold()
    if len(content) < 2:
        return None
    return (
        _as_text(notice.get("guild_id")),
        _as_text(notice.get("feed_id")),
        content,
    )


class TencentChannelAdapter(BaseAdapter):
    """腾讯频道轮询适配器。"""

    adapter_name = "tencent_channel_adapter"
    name = "tencent_channel_adapter"
    adapter_version = "1.0.0"
    adapter_author = "小小辉"
    #: 直接写字面量：工具链按 AST 取字面量，写成 ``= 某个变量`` 会被当成空描述
    description = "腾讯频道（QQ 频道）轮询适配器：互动通知 → 统一消息管线 → CLI 自动回复"
    platform = PLATFORM
    dependencies: list[str] = ["tencent_channel:service:channel_cli"]

    # ── 初始化 ──────────────────────────────────────────────

    def __init__(self, core_sink: Any = None, plugin: Any = None, **kwargs: Any) -> None:
        """读取配置并准备状态。

        注意：框架在 ``__init__`` 阶段传入的 ``core_sink`` 可能是 ``None``
        （SinkManager 随后才注入），因此这里不做任何需要 core_sink 的事情；
        需要 core_sink 的初始化放在 ``on_adapter_loaded``。
        """
        super().__init__(core_sink, plugin=plugin, **kwargs)

        config = getattr(plugin, "config", None)
        self._config = config
        self._cli: TencentChannelCli = build_cli_client(config, logger=get_logger("tencent_channel.cli"))

        # plugin.*（插件总开关）
        self._plugin_enabled = bool(cfg_get(config, "plugin", "enabled", True))
        # channel.*
        self._enabled = bool(cfg_get(config, "channel", "poll", True))
        #: 待解析的频道引用（channel.guilds：频道号或真实 ID，可多个）
        self._guild_refs = configured_guild_refs(config)
        #: 主频道 guild_id：解析完成前先取配置里「本来就是纯数字」的那一个兜底
        self._guild_id = next((ref for ref in self._guild_refs if looks_like_guild_id(ref)), "")
        #: 解析后的真实 guild_id 列表（第一个 = 主频道，工具/动作的默认频道）
        self._guilds: list[str] = []
        self._channel_id = str(cfg_get(config, "channel", "channel_id", ""))
        self._poll_interval = max(5.0, _as_float(cfg_get(config, "channel.notices", "poll_interval", 600.0), 600.0))
        self._page_num = max(1, _as_int(cfg_get(config, "channel.notices", "page_num", 20), 20))
        self._max_notices = max(1, _as_int(cfg_get(config, "channel.notices", "max_per_poll", 20), 20))
        self._inject_history = bool(cfg_get(config, "channel.notices", "inject_history", False))
        self._enable_reply = bool(cfg_get(config, "reply", "enabled", True))
        raw_types = cfg_get(config, "channel.notices", "types", list(DEFAULT_NOTICE_TYPES))
        self._notice_types = [
            str(item).strip().lower() for item in (raw_types or list(DEFAULT_NOTICE_TYPES)) if str(item).strip()
        ] or list(DEFAULT_NOTICE_TYPES)
        # 新帖子监听：帖子本身不产生互动通知，只能额外轮询帖子列表发现
        self._watch_feeds = bool(cfg_get(config, "channel.feeds", "enabled", False))
        self._feed_interval = max(
            15.0, _as_float(cfg_get(config, "channel.feeds", "poll_interval", 600.0), 600.0)
        )
        self._feed_page_num = max(1, _as_int(cfg_get(config, "channel.feeds", "page_num", 10), 10))
        self._feed_skip_self = bool(cfg_get(config, "channel.feeds", "skip_self", True))
        # 频道级会话：整个频道当一个群
        self._channel_as_group = bool(cfg_get(config, "channel", "channel_as_group", False))
        # 评论轮询：把房间里的对话（含别人之间的评论/回复）也注入
        self._watch_comments = bool(cfg_get(config, "channel.comments", "enabled", False))
        self._comment_interval = max(
            15.0, _as_float(cfg_get(config, "channel.comments", "poll_interval", 600.0), 600.0)
        )
        self._comment_page_num = max(
            1, _as_int(cfg_get(config, "channel.comments", "page_num", 5), 5)
        )
        self._comment_reply_list_num = max(
            0, min(10, _as_int(cfg_get(config, "channel.comments", "reply_list_num", 1), 1))
        )
        self._comment_max_age_hours = max(
            0.0, _as_float(cfg_get(config, "channel.comments", "max_age_hours", 72.0), 72.0)
        )
        self._comment_skip_self = bool(cfg_get(config, "channel.comments", "skip_self", True))
        _policy = str(cfg_get(config, "channel", "reply_target_policy", "latest") or "").strip().lower()
        self._reply_target_policy = _policy if _policy in ("latest", "oldest") else "latest"
        # 凭证生命周期（cli.*）：开机未登录时打印二维码、后台等扫码
        self._auto_login = bool(cfg_get(config, "cli.login", "auto_qrcode", False))
        self._logout_on_startup = bool(cfg_get(config, "cli.login", "logout_on_startup", False))
        self._login_wait_seconds = max(
            30.0, _as_float(cfg_get(config, "cli.login", "wait_seconds", 600.0), 600.0)
        )
        self._login_poll_interval = max(
            3.0, _as_float(cfg_get(config, "cli.login", "poll_interval", 10.0), 10.0)
        )
        #: 私信来源频道配置原值（同样支持频道号，启动时解析）
        self._dm_source_raw = str(cfg_get(config, "channel", "dm_source_guild_id", ""))
        self._dm_source_guild_id = self._dm_source_raw or self._guild_id
        self._self_tiny_id = str(cfg_get(config, "channel", "self_tiny_id", ""))
        #: 本账号昵称（get-user-info 探测，用于按昵称反查 tiny_id）
        self._self_nickname = ""
        #: 本账号「频道昵称」（member_name，可能与全局昵称不同）
        self._self_member_name = ""
        self._bot_id = str(cfg_get(config, "channel", "bot_id", ""))
        self._bot_name = str(cfg_get(config, "channel", "bot_name", "QQ频道助手"))
        #: Bot 的 QQ 号（仅用于 bot_id 与日志；接口不返回 QQ 号）
        self._bot_qq = str(cfg_get(config, "channel", "bot_qq", ""))
        #: 自动取到的版块名（日志用）
        self._section_name = ""
        state_path = str(cfg_get(config, "channel", "state_path", "") or "")
        self._state_path: Path | None = Path(state_path) if state_path else None

        # reply.*
        self._max_length = max(1, _as_int(cfg_get(config, "reply", "max_length", 1000), 1000))
        self._reply_to_comment = bool(cfg_get(config, "reply", "reply_to_comment", True))
        self._enrich = bool(cfg_get(config, "reply", "enrich_comment_context", True))

        # log.*
        self._log_raw = bool(cfg_get(config, "log", "raw_notices", False))
        self._log_summary = bool(cfg_get(config, "log", "poll_summary", True))

        # 运行时状态
        self._watermark = NoticeWatermark()
        self._ctx: dict[str, dict[str, Any]] = {}
        self._ctx_by_stream: dict[str, dict[str, Any]] = {}
        #: 帖子详情缓存（feed_id → (取回时间, 详情)），用于补发送者昵称
        self._detail_cache: dict[str, tuple[float, Mapping[str, Any]]] = {}
        self._recent_out: dict[str, float] = {}
        #: 跨路径同内容去重：(feed_id, 正文) → (来源路径, 记录时间)
        self._recent_content: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._polling = False
        self._cli_ok = False
        self._paused_reason = ""
        self._first_poll = True
        self._poll_task_id: str | None = None
        self._poll_task: Any = None
        self._last_hint = ""
        #: 等待扫码登录的后台任务
        self._login_task_id: str | None = None
        #: token 直登凭证（cli.login.token / cli.login.token_file；None=未配置/解析失败）
        self._login_credential: LoginCredential | None = None
        #: 下一次新帖子轮询的时间点（monotonic）
        self._next_feed_poll = 0.0
        #: 下一次评论轮询的时间点（monotonic）
        self._next_comment_poll = 0.0
        #: 最近一次拿到的帖子列表（供评论轮询复用，避免重复调 CLI）
        #: 每个频道最近一次拉到的帖子列表（评论轮询复用；键 = guild_id）
        self._recent_feeds: dict[str, list[dict[str, Any]]] = {}
        #: 待回复队列：会话 key → 尚未被回复的入站目标 ctx（频道级会话下可能有多个）
        self._pending_targets: dict[str, list[dict[str, Any]]] = {}
        #: 会话 key → 最近一次**成功回复**用过的目标：队列空时回退用它（一回合多条消息的场景）
        self._last_target: dict[str, dict[str, Any]] = {}

    # ── 生命周期 ────────────────────────────────────────────

    async def on_adapter_loaded(self) -> None:
        """加载钩子：解析频道（支持频道号）、校验 CLI 与登录状态，然后启动轮询任务。"""
        logger.info(
            f"腾讯频道适配器已加载（platform={self.platform}，CLI={self._cli.describe()}）"
        )
        if not self._plugin_enabled:
            logger.warning(
                "plugin.enabled=false：腾讯频道插件整体停用（不轮询、不登录、工具与动作也不可用）"
            )
            return
        self._load_state()
        self._login_credential = self._resolve_login_credential()

        # 选项：启动时清掉上次残留的凭证（强杀/断电不会走 ON_STOP，只能在这里兜底）。
        # 放在 enabled / guild 检查之前：只要插件启动就执行，保证凭证是「单次运行」的。
        # 配了 token 直登也没关系：清完会在 preflight 未登录时自动写回（见 _try_token_login）。
        if self._logout_on_startup:
            await self._clear_stale_login()

        if not self._enabled:
            logger.warning("channel.poll=false：只提供工具/动作，不轮询通知")
            return
        if not self._guild_refs:
            logger.warning(
                "channel.guilds 未配置，跳过通知轮询。"
                "可填频道号（形如 pd20589127，插件会自动解析成真实 ID）或真实频道 ID，支持多个；"
                "也可用 `manage get-my-join-guild-info` 查看自己的频道"
            )
            return
        # 先确认 CLI 可用与登录状态（错误提示更精确），再解析频道号
        if not await self._preflight():
            if self._auto_login and self._paused_reason == "auth":
                # 未登录：打印授权链接与二维码，后台等扫码，成功后自动继续启动
                await self._start_login_flow()
            return
        if not await self._resolve_guild_ids():
            return
        # 版块 ID（发帖/评论用）：QQ 客户端里看不到，留空就自动取
        await self._ensure_section_id()
        # tiny_id（do-reply 的 replier_id）：CLI 不直接给，退一步按昵称在频道里搜自己
        if not self._self_tiny_id:
            self._self_tiny_id = await self._probe_tiny_id_from_members()
        if not self._self_tiny_id:
            logger.warning(
                "未能确定本账号 tiny_id：评论级回复会因缺 replier_id 降级为帖内评论。"
                "可在配置 channel.self_tiny_id 显式指定（用 `manage guild-member-search "
                "--guild-id <频道ID> --keyword <你的昵称>` 取返回里的 tinyid）"
            )
        logger.info(
            "适配器就绪："
            f"频道={'、'.join(self._guilds)}"
            f"，版块={self._channel_id or '（未取到，发帖/评论可能失败）'}"
            + (f"（{self._section_name}）" if self._section_name else "")
            + f"，账号=「{self._self_nickname or '未知'}」"
            + (f"，tiny_id={self._self_tiny_id}" if self._self_tiny_id else "")
            + (f"，bot_qq={self._bot_qq}" if self._bot_qq else "")
        )
        self._start_poll_task()

    async def _ensure_section_id(self) -> str:
        """确定「默认发帖版块」（``channel.channel_id`` / ``channel.section_name``）。

        优先级：``channel_id``（版块 ID，显式）→ ``section_name``（版块名，显式）→
        自动取「全部」版块 → 自动取第一个版块。

        为什么要有它：**版块 ID 在 QQ 客户端里看不到**（频道 ID 也看不到）。插件把
        「频道号 → 真实 guild_id」「昵称 → tiny_id」都自动化了，版块同样自动取，
        于是最小配置只需要：频道号 + bot 昵称 + bot QQ 号。

        取不到时只警告：读操作不受影响，发帖/评论会缺 channel_id（下一轮轮询会再试）。
        """
        if self._channel_id:
            return self._channel_id
        if not self._guild_id:
            return ""
        # 解析规则与动作侧共用（ID → 配置里的名字 → 自动取「全部」→ 第一个）
        channel_id, error = await resolve_section_target(self._cli, self._config, self._guild_id)
        if not channel_id:
            logger.warning(
                f"默认发帖版块未确定：{error}；读操作不受影响，发帖/评论会缺 channel_id（下一轮会再试）"
            )
            return ""
        self._channel_id = channel_id
        entries = await self._list_sections()
        if entries:
            self._section_name = next(
                (
                    str(item.get("channel_name") or "")
                    for item in entries
                    if str(item.get("channel_id")) == channel_id
                ),
                self._section_name,
            )
            names = "、".join(
                f"{item.get('channel_name') or '（无名）'}({item.get('channel_id')})" for item in entries[:8]
            )
            logger.info(
                f"默认发帖版块：{self._section_name or channel_id}（{channel_id}）；"
                f"本频道共 {len(entries)} 个版块 —— {names}"
                "；可用 channel.section_name 指定默认版块，或让 bot 发帖时把版块名传给 channel_id"
            )
        else:
            logger.info(f"默认发帖版块：{channel_id}")
        return self._channel_id

    async def _list_sections(self) -> list[dict[str, str]]:
        """拉一次频道版块列表（只为日志/展示；失败返回空列表）。"""
        try:
            result = await self._cli.get_channel_list(self._guild_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"拉取版块列表失败（忽略）：{exc}")
            return []
        if not getattr(result, "ok", False):
            return []
        payload = result.data if result.data is not None else result.raw
        return iter_channel_entries(payload)

    # ── 开机登录（可选）──────────────────────────────────────

    def _resolve_login_credential(self) -> LoginCredential | None:
        """解析 token 直登凭证：配置项优先，其次在 logout 清除**之前**抓取本机凭据存储。

        第二步是「扫过一次码的机器以后永不扫码」的关键：``logout_on_startup``
        会清掉凭据存储，若配置里没填令牌，就先抓一份留给 preflight 失败时回注。
        可用 ``cli.login.capture_keychain=false`` 关掉（恢复严格的一次性会话语义）。
        """
        try:
            credential = resolve_login_credential(self._config)
        except TokenLoginError as exc:
            logger.warning(f"token 直登配置无效（忽略，回退扫码流程）：{exc}")
            credential = None
        if credential is None and bool(cfg_get(self._config, "cli.login", "capture_keychain", True)):
            try:
                token, device_id = read_stored_login()
            except TokenLoginError as exc:
                logger.debug(f"读取本机凭据存储失败（跳过抓取）：{exc}")
                token, device_id = "", ""
            if token:
                credential = LoginCredential(
                    token=token, device_id=device_id, source="本机凭据存储（logout_on_startup 清除前抓取）"
                )
        if credential is not None:
            if credential.source.startswith("本机凭据存储"):
                logger.warning(
                    f"已启用 token 直登：token={credential.masked}（来源：{credential.source}）—— "
                    "本机凭据存储是**全机器共享**的：同机跑多个实例时这可能是别的实例登录的账号，"
                    "建议给本实例配置 cli.login.token_file（见 README「多实例机器」）"
                )
            else:
                logger.info(f"已启用 token 直登：token={credential.masked}（来源：{credential.source}）")
        return credential

    @staticmethod
    def _read_stored_credential() -> tuple[str, str]:
        """读本机凭据存储里的 ``(token, device_id)``；读不到返回空串（用于比对与回滚）。"""
        try:
            return read_stored_login()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"读取本机凭据存储失败：{exc}")
            return "", ""

    def _uses_shared_cli_store(self) -> bool:
        """当前客户端是否依赖 **全机器共享** 的 CLI 凭据存储（gateway 模式不依赖）。"""
        return str(getattr(self._cli, "mode", "")).strip().lower() != "gateway"

    async def _try_token_login(self, logged_out: bool = True) -> bool:
        """把配置的访问令牌写入 CLI 的凭证存储（token 直登）；成功返回 True。

        Args:
            logged_out: True = 当前未登录（原来的兜底路径）；False = 已登录但用的是
                别的令牌，需要换成配置的令牌。只影响日志措辞。

        写完由调用方用 ``login status`` 复验；令牌失效时调用方负责回滚。
        """
        credential = self._login_credential
        if credential is None:
            return False
        reason = "CLI 未登录" if logged_out else "本机登录的是另一个令牌"
        logger.info(
            f"{reason}，尝试 token 直登（token={credential.masked}，来源：{credential.source}）…"
        )
        try:
            written = inject_login_credential(credential)
        except TokenLoginError as exc:
            logger.warning(f"token 直登失败（回退扫码/人工流程）：{exc}")
            return False
        logger.info(f"已把访问令牌写入系统密钥链：{', '.join(written)}，正在验证登录…")
        return True

    async def _restore_credential(self, token: str, device_id: str) -> None:
        """把注入前的凭证写回去（配置的令牌不可用时，别把原本有效的登录弄坏）。"""
        if not token:
            return
        try:
            inject_login_credential(
                LoginCredential(token=token, device_id=device_id, source="回滚（原本机凭证）")
            )
        except TokenLoginError as exc:
            logger.error(f"回滚本机凭证写入失败（{exc}）：恢复登录请手动扫码")
            return
        logger.warning("已回滚到注入前的本机凭证")

    async def _ensure_configured_identity(self, login: CliResult) -> CliResult:
        """让「配置的令牌」成为本实例的登录身份，而不是被本机已有的登录顶掉。

        为什么需要：CLI 的凭证存储（Windows 凭据管理器 ``qq-cli:token``）是**全机器共享**的。
        同机跑两个实例时，另一个实例登录过一次，``login status`` 就会成功 ——
        配置里的 ``cli.login.token`` / ``token_file`` 于是被静默忽略，本实例会以
        **另一个 bot 的账号**运行（日志里的账号昵称、tiny_id 都是别人的）。

        规则：

        - **gateway 模式直接跳过**：它的令牌是配置里读的、按请求发送，不经过 CLI 凭据存储，
          也就不会被别的实例顶掉（同机多账号建议用这个模式）；
        - 存储里已经是同一份令牌 → 什么都不做；
        - 未登录 → 注入（原有兜底行为）；
        - 已登录但存的是**别的**令牌 → 换成配置的；换完登录不上则回滚原凭证。
        """
        credential = self._login_credential
        if credential is None:
            return login
        if self._uses_shared_cli_store() is False:
            logger.debug("gateway 模式不经过 CLI 凭据存储，跳过身份校验/注入")
            return login

        stored_token, stored_device = self._read_stored_credential()
        if stored_token and stored_token == credential.token:
            logger.debug("本机凭据存储里已是配置的令牌，无需注入")
            return login

        if login.ok and stored_token:
            logger.warning(
                f"本机凭据存储里是**另一个**令牌（共 {len(stored_token)} 位），"
                f"与 cli.login 配置的令牌（{credential.masked}）不一致 —— "
                "将改用配置的令牌，避免本实例以别的账号（别的 bot）身份运行"
            )
        if not await self._try_token_login(logged_out=not login.ok):
            return login

        verified = await self._cli.login_status()
        self._note_hints(verified)
        if verified.ok:
            logger.info(
                "token 直登验证通过：已用配置的访问令牌登录（未扫码）"
                if not login.ok
                else "已改用配置的令牌登录（token 直登验证通过）"
            )
            return verified

        if login.ok and stored_token:
            await self._restore_credential(stored_token, stored_device)
            restored = await self._cli.login_status()
            self._note_hints(restored)
            if restored.ok:
                logger.warning(
                    f"配置的令牌不可用（{verified.error}），已回滚到原本的登录："
                    "本实例仍会以那个账号运行 —— 请更新 cli.login.token / token_file，"
                    "或清掉本机凭证后重启"
                )
                return restored
        return verified

    async def _clear_stale_login(self) -> None:
        """启动时清除上次残留的登录凭证（``cli.logout_on_startup``）。

        为什么需要它：只有**优雅关机**才走得到 ``ON_STOP`` 的退登；被强杀 /
        断电 / 启动器直接结束进程时事件不会发出，凭证会留在本机。这里在下次
        启动时兜底清掉，配合 ``cli.login.auto_qrcode`` 就得到「每次开机都要
        重新扫码」的会话式凭证。失败只警告，不影响启动。
        """
        if not self._uses_shared_cli_store():
            logger.debug("gateway 模式不使用 CLI 凭据存储，跳过 cli.login.logout_on_startup")
            return
        try:
            client = build_cli_client(self._config, logger=logger, timeout=15.0)
            result = await client.login_logout()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"启动清理登录凭证异常（继续启动）：{exc}")
            return
        if result.ok:
            logger.info("已清除上次残留的 CLI 登录凭证（cli.logout_on_startup=true）")
        else:
            logger.warning(f"启动清理登录凭证失败（继续启动）：{result.error}")

    async def _start_login_flow(self) -> None:
        """未登录时：打印授权链接与二维码，并后台等待扫码。

        需要 ``cli.login.auto_qrcode=true``。二维码优先用 CLI 落盘的 PNG 渲染成字符画，
        渲染失败也能靠授权链接完成登录（日志里同时给出链接与图片路径）。
        """
        if not self._uses_shared_cli_store():
            logger.error(
                "gateway 模式无法扫码登录：请先用 CLI 扫码一次，跑 "
                "plugins/tencent_channel/export_login_token.py 导出令牌，"
                "再填进 cli.login.token_file（或 cli.login.token）"
            )
            return
        start = await self._cli.login_start()
        if not start.ok:
            logger.error(
                f"发起登录失败（{start.error}）：请手动执行 `tencent-channel-cli.cmd login --json` 扫码"
            )
            return

        uri, source = describe_qr_source(start.data)
        logger.warning(
            "腾讯频道 CLI 未登录：请用手机 QQ 扫码完成授权（插件只在本次启动期间等待）"
        )
        if uri:
            logger.warning(f"授权链接（浏览器直接打开即可）：{uri}")
        if source:
            logger.warning(f"二维码图片：{source}")
        art = render_qr_ascii(source)
        if art:
            logger.info("\n" + art)
        else:
            logger.info("（二维码字符画渲染失败，用上面的链接或图片即可）")

        manager = resolve_task_manager()
        try:
            info = manager.create_task(
                self._await_login(), name="tencent_channel_login", daemon=True
            )
            self._login_task_id = getattr(info, "task_id", None)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"启动扫码等待任务失败：{exc}")

    async def _await_login(self) -> None:
        """后台等待扫码完成；成功后接着走 preflight → 解析频道 → 启动轮询。"""
        deadline = time.monotonic() + self._login_wait_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(self._login_poll_interval)
            try:
                result = await self._cli.login_poll_token()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"等待扫码时轮询异常（继续等）：{exc}")
                continue
            if result.ok:
                logger.info("扫码登录成功，继续启动腾讯频道轮询…")
                self._paused_reason = ""
                if not await self._preflight():
                    logger.warning("登录后自检仍未通过，已停止启动流程")
                    return
                if not await self._resolve_guild_ids():
                    return
                if not self._self_tiny_id:
                    self._self_tiny_id = await self._probe_tiny_id_from_members()
                self._start_poll_task()
                return
            logger.debug(f"尚未完成扫码（{result.kind}）：{result.error}")
        logger.error(
            "等待扫码登录超时：本次不启动频道轮询；重启程序会再次打印二维码，"
            "也可以手动执行 `tencent-channel-cli.cmd login --json` 后重启"
        )

    async def _resolve_guild_ids(self) -> bool:
        """把配置里的频道引用（真实 ID 或频道号）**逐个**解析成真实 guild_id。

        支持多频道：``channel.guilds`` 的每一项都会解析，失败项跳过并告警；
        第一个解析成功的作为「主频道」（工具/动作不显式指定频道时的默认值）。
        返回 False 表示应跳过轮询。
        """
        refs = list(self._guild_refs)
        if not refs:
            logger.warning("未配置监听频道（channel.guilds），跳过轮询")
            return False

        resolved: list[str] = []
        for ref in refs:
            # resolve_guild 自己区分「纯数字 = 真实 ID」与「频道号 = 需要换算」
            resolution = await resolve_guild(self._cli, guild_id=ref, logger=logger)
            if not resolution.ok:
                logger.error(f"频道 {ref} 解析失败（已跳过）：{resolution.message}")
                continue
            if resolution.guild_id != ref:
                logger.info(f"频道号已解析：{ref} → guild_id={resolution.guild_id}")
            if resolution.guild_id not in resolved:
                resolved.append(resolution.guild_id)

        if not resolved:
            self._paused_reason = "guild_unresolved"
            return False

        self._guilds = resolved
        self._guild_id = resolved[0]
        if len(resolved) > 1:
            logger.info(f"多频道监听已启用：共 {len(resolved)} 个频道 → {', '.join(resolved)}")

        # 私信来源频道同样支持填频道号
        dm_raw = self._dm_source_raw or self._guild_id
        if dm_raw != self._guild_id:
            dm_resolution = await resolve_guild(self._cli, guild_id=dm_raw, logger=logger)
            if not dm_resolution.ok:
                logger.warning(f"私信来源频道未能解析（沿用原值）：{dm_resolution.message}")
            self._dm_source_guild_id = dm_resolution.guild_id if dm_resolution.ok else dm_raw
        else:
            self._dm_source_guild_id = self._guild_id
        return True

    async def on_adapter_unloaded(self) -> None:
        """卸载钩子：取消轮询任务并落盘水位线。"""
        manager = resolve_task_manager()
        if self._login_task_id:
            try:
                manager.cancel_task(self._login_task_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"取消扫码等待任务失败：{exc}")
            self._login_task_id = None
        await self._stop_poll_task()
        self._ctx.clear()
        self._ctx_by_stream.clear()
        self._recent_out.clear()
        self._save_state()
        logger.info("腾讯频道适配器已卸载")

    async def _preflight(self) -> bool:
        """校验 CLI 可用性与登录状态（不代跑登录、不读取凭证）。"""
        version = await self._cli.version()
        if not version.ok:
            logger.error(f"tencent-channel-cli 不可用：{version.error}")
            logger.error(
                "请先安装 `npm install -g tencent-channel-cli`，"
                "并确认配置 cli.path / cli.mode 指向可执行入口（Windows 建议用 .cmd 或 node 直调 .js）"
            )
            self._paused_reason = "cli_missing"
            return False
        logger.info(f"tencent-channel-cli 版本信息：{self._plain(version)}")

        login = await self._cli.login_status()
        self._note_hints(login)
        # 配置了令牌就让它成为本实例的身份：CLI 凭证存储是全机器共享的，
        # 同机别的实例登录过时 login status 会成功，配置的令牌会被静默顶掉。
        login = await self._ensure_configured_identity(login)
        if not login.ok:
            if self._login_credential is not None:
                logger.error(
                    f"CLI 未登录或鉴权失败（{login.error}），token 直登已尝试但未通过："
                    "请确认 cli.login.token / cli.login.token_file 里的令牌是否过期"
                    "（重跑 plugins/tencent_channel/export_login_token.py 重新导出），"
                    "或手动执行 `tencent-channel-cli.cmd login --json` 扫码授权后重启。"
                )
            else:
                logger.error(
                    f"CLI 未登录或鉴权失败（{login.error}）。请手动执行 `tencent-channel-cli.cmd login --json` 扫码授权，"
                    "完成后重启 MoFox Code。插件不会代跑登录，也不会读取/打印你的凭证"
                    "（也可配置 cli.login.token_file 启用 token 直登，免扫码恢复）。"
                )
            self._paused_reason = "auth"
            return False

        self._cli_ok = True
        if not self._self_tiny_id or not self._self_nickname:
            # 实测（CLI 1.0.10）：login status 不返回 tiny_id；网关模式的 get-user-info 会返回。
            # 这里顺手试一把 + 记下昵称/频道昵称，真正的兜底在 guild_id 解析之后
            # （见 _probe_tiny_id_from_members，会按这些昵称去频道里搜自己）。
            if not self._self_tiny_id:
                self._self_tiny_id = self._probe_self_tiny_id(login)
            profile = await self._cli.get_user_info()
            if profile.ok:
                if not self._self_tiny_id:
                    self._self_tiny_id = self._probe_self_tiny_id(profile)
                self._self_nickname = self._self_nickname or self._probe_nickname(profile)
                self._self_member_name = self._self_member_name or self._probe_member_name(profile)
            if self._self_tiny_id:
                logger.info("已探测到本账号 tiny_id（用于 do-reply 的 replier_id）")
        await self._log_account_identity()
        return True

    async def _log_account_identity(self) -> None:
        """把「当前登录的是哪个账号」写进启动日志。

        同机多实例 / 多账号时最容易搞混的就是这一点：日志里迟早会打印「已按昵称「X」
        探测到本账号 tiny_id」，但那时已经过了一轮解析，不如启动就说清楚。
        只在本轮还没拿到昵称时查一次 ``get-user-info``（best-effort，失败不影响启动）。
        """
        if not self._self_nickname:
            try:
                profile = await self._cli.get_user_info()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"查询登录账号资料失败（忽略）：{exc}")
                return
            if profile.ok:
                self._self_nickname = self._probe_nickname(profile)
                self._self_member_name = self._self_member_name or self._probe_member_name(profile)
                if not self._self_tiny_id:
                    self._self_tiny_id = self._probe_self_tiny_id(profile)
        if self._self_nickname:
            suffix = f"，tiny_id={self._self_tiny_id}" if self._self_tiny_id else ""
            if self._self_member_name and self._self_member_name != self._self_nickname:
                suffix += f"，频道昵称「{self._self_member_name}」"
            logger.info(f"当前登录账号：昵称「{self._self_nickname}」{suffix}")
        if self._bot_qq and not self._self_tiny_id:
            logger.debug(
                f"已配置 bot_qq={self._bot_qq}：接口不返回 QQ 号，无法用它反查 tiny_id（只用于 bot_id/日志）"
            )

    # ── 轮询 ────────────────────────────────────────────────

    def _start_poll_task(self) -> None:
        """通过框架 task_manager 启动轮询任务。"""
        if self._polling:
            return
        self._paused_reason = ""
        try:
            manager = resolve_task_manager()
            info = manager.create_task(self._poll_loop(), name="tencent_channel_poll", daemon=True)
            self._poll_task_id = getattr(info, "task_id", None)
            self._poll_task = getattr(info, "task", None)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"启动通知轮询任务失败：{exc}", exc_info=True)
            return
        self._polling = True
        # 三条轮询路径各自的间隔都在启动日志里说明（改配置后一眼能看出生效没生效）
        extras: list[str] = []
        if self._watch_feeds:
            extras.append(f"新帖={_interval_text(self._feed_interval)}")
        if self._watch_comments:
            extras.append(f"评论={_interval_text(self._comment_interval)}")
        logger.info(
            f"通知轮询已启动：{len(self._guilds)} 个频道={','.join(self._guilds)}，"
            f"间隔={_interval_text(self._poll_interval)}"
            + (f"（{'，'.join(extras)}）" if extras else "")
            + f"，单轮最多注入 {self._max_notices} 条，监听类型={','.join(self._notice_types)}"
        )

    async def _stop_poll_task(self) -> None:
        """取消轮询任务。"""
        self._polling = False
        task_id, self._poll_task_id = self._poll_task_id, None
        task, self._poll_task = self._poll_task, None
        if task_id:
            try:
                resolve_task_manager().cancel_task(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"取消轮询任务失败：{exc}")
        elif task is not None:
            try:
                task.cancel()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"取消轮询任务失败：{exc}")

    async def _poll_loop(self) -> None:
        """轮询主循环：单轮异常不影响整体，鉴权/CLI 缺失则暂停等待人工处理。"""
        logger.debug("轮询循环开始")
        consecutive_failures = 0
        try:
            while True:
                try:
                    keep_going = await self._poll_once()
                    consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    consecutive_failures += 1
                    logger.error(
                        f"轮询异常（连续第 {consecutive_failures} 次）：{exc}", exc_info=True
                    )
                    keep_going = consecutive_failures < 10
                if not keep_going:
                    self._polling = False
                    logger.warning(
                        f"通知轮询已暂停（{self._paused_reason or '未知原因'}），处理完请重启 MoFox Code"
                    )
                    return
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            logger.info("通知轮询任务已取消")
            raise

    async def _poll_once(self) -> bool:
        """拉取一轮通知（**逐个频道**）；返回 False 表示应暂停轮询。"""
        if not self._channel_id:
            # 启动时没取到版块 ID（网络抖动等）：每轮重试一次，拿到就缓存住
            await self._ensure_section_id()
        notices: list[dict[str, Any]] = []
        paused_reason = ""
        for guild_id in self._guilds:
            result = await self._cli.get_notices(guild_id=guild_id, page_num=self._page_num)
            if not result.ok:
                logger.warning(
                    f"拉取互动通知失败（频道 {guild_id}）：{result.error}（{result.hint()}）"
                )
                if result.kind in ("auth", "not_found"):
                    paused_reason = result.kind
                continue
            self._note_hints(result)
            raw_notices = extract_notices(result.data)
            if self._log_raw and raw_notices:
                logger.info(
                    f"原始通知样本（截断）：{json.dumps(raw_notices[:3], ensure_ascii=False, default=str)[:1500]}"
                )
            for item in raw_notices:
                notice = normalize_notice(item, self_names=self._nickname_candidates())
                # 真实通知里 guild_id 经常是空的：补上本次轮询的频道，
                # 这样去重键、会话路由、出站目标都会带上正确的频道（多频道时尤其重要）
                if not notice.get("guild_id"):
                    notice["guild_id"] = guild_id
                if not notice.get("source_guild_id"):
                    notice["source_guild_id"] = guild_id
                notices.append(notice)

        if paused_reason and not notices:
            self._paused_reason = paused_reason
            return False

        # 统一去重键：让「互动通知」与「评论轮询」发现同一条评论/回复时只注入一次
        for notice in notices:
            notice["id"] = canonical_notice_id(notice)
        fresh = self._watermark.filter_new(notices)

        first = self._first_poll
        self._first_poll = False
        if first and not self._inject_history:
            logger.info(
                f"首次轮询建立基线：记下 {len(fresh)} 条历史通知"
                "（默认不注入，避免冷启动把历史互动一次性灌给 LLM）"
            )
            await self._poll_new_feeds(baseline=True)
            await self._poll_comments(baseline=True)
            self._save_state()
            return True

        actionable = [notice for notice in fresh if is_actionable(notice, self._notice_types)]
        ignored = len(fresh) - len(actionable)
        if len(actionable) > self._max_notices:
            logger.warning(
                f"本轮新增可处理通知 {len(actionable)} 条，超过上限 {self._max_notices} 条："
                f"只注入最新的 {self._max_notices} 条"
            )
            actionable = actionable[-self._max_notices :]

        if self._log_summary:
            logger.info(
                f"轮询统计：{len(self._guilds)} 个频道，拉取 {len(notices)} 条，新增 {len(fresh)} 条，"
                f"注入 {len(actionable)} 条，忽略 {ignored} 条"
            )

        for notice in actionable:
            await self._inject(notice, source="notice")
        await self._poll_new_feeds()
        await self._poll_comments()
        self._save_state()
        return True

    async def _poll_new_feeds(self, *, baseline: bool = False) -> None:
        """轮询频道主页帖子，把**新帖子**也注入给 LLM。

        帖子本身不会产生互动通知（只有被 @/评论/回复才有），所以「别人发帖但没 @ 我」
        这种情况只能靠轮询帖子列表发现。转成同一套通知结构后，去重水位线、注入管线、
        出站回复（评论该帖子）全部复用现成逻辑。
        """
        if not self._watch_feeds or not self._guilds:
            return
        now = time.monotonic()
        if not baseline and now < self._next_feed_poll:
            return
        self._next_feed_poll = now + self._feed_interval

        notices: list[dict[str, Any]] = []
        for guild_id in self._guilds:
            result = await self._cli.get_guild_feeds(
                guild_id=guild_id, get_type=2, count=self._feed_page_num
            )
            if not result.ok:
                logger.warning(f"拉取频道帖子列表失败（频道 {guild_id}）：{result.error}")
                continue
            feeds = extract_feeds(result.data)
            # 供「评论轮询」复用：避免为同一批帖子再拉一次列表（按频道分开存）
            self._recent_feeds[guild_id] = feeds
            names = self._nickname_candidates()
            notices.extend(
                notice
                for notice in (
                    notice_from_feed(feed, guild_id=guild_id, self_names=names) for feed in feeds
                )
                if notice.get("id")
            )
        if self._feed_skip_self and self._self_tiny_id:
            skipped = [
                n for n in notices if _as_text(n.get("feed_author_id")) == self._self_tiny_id
            ]
            if skipped:
                notices = [n for n in notices if _as_text(n.get("feed_author_id")) != self._self_tiny_id]
                logger.debug(f"跳过自己发的 {len(skipped)} 条帖子（channel.feeds.skip_self=true）")

        fresh = self._watermark.filter_new(notices)
        if baseline:
            logger.info(f"新帖子基线：记下 {len(fresh)} 条（默认不注入）")
            return
        if not fresh:
            return

        actionable = fresh[-self._max_notices :]
        if self._log_summary:
            logger.info(
                f"新帖子轮询：拉取 {len(notices)} 条，新增 {len(fresh)} 条，注入 {len(actionable)} 条"
            )
        for notice in actionable:
            await self._inject(notice, source="feed")

    async def _poll_comments(self, *, baseline: bool = False) -> None:
        """轮询帖子评论区，把房间里的对话也注入给 LLM（群聊式上下文）。

        「互动通知」只会带来指向机器人的评论/回复；想让频道像群聊，就必须把**别人之间**
        的评论/回复也读进来。做法：对最近 N 条有评论的帖子调
        ``feed get-feed-comments``（可带 ``--reply-list-num`` 预加载楼中楼），
        转成同一套通知结构后复用去重水位线与注入管线。

        去重键与互动通知路径一致（``comment|<feed>|<id>`` / ``reply|<feed>|<id>``），
        所以同一条评论不会被注入两次。
        """
        if not self._watch_comments or not self._guilds:
            return
        now = time.monotonic()
        if not baseline and now < self._next_comment_poll:
            return
        self._next_comment_poll = now + self._comment_interval

        max_age_seconds = self._comment_max_age_hours * 3600 if self._comment_max_age_hours > 0 else 0.0
        now_ts = time.time()
        checked = 0
        collected: list[dict[str, Any]] = []

        for guild_id in self._guilds:
            feeds = list(self._recent_feeds.get(guild_id, []))
            if not feeds:
                # 例如 channel.feeds.enabled=false：评论轮询自己拉一次帖子列表
                result = await self._cli.get_guild_feeds(
                    guild_id=guild_id, get_type=2, count=self._comment_page_num
                )
                if not result.ok:
                    logger.warning(f"评论轮询：拉取帖子列表失败（频道 {guild_id}）：{result.error}")
                    continue
                feeds = extract_feeds(result.data)
                self._recent_feeds[guild_id] = feeds

            for feed in feeds[: self._comment_page_num]:
                feed_id = _as_text(feed.get("feed_id"))
                if not feed_id:
                    continue
                try:
                    comment_count = int(feed.get("comment_count") or 0)
                except (TypeError, ValueError):
                    comment_count = 0
                if comment_count <= 0:
                    continue  # 没有评论的帖子省一次 CLI 调用
                if max_age_seconds > 0:
                    try:
                        feed_ts = float(feed.get("create_time_raw") or 0)
                    except (TypeError, ValueError):
                        feed_ts = 0.0
                    if feed_ts > 0 and now_ts - feed_ts > max_age_seconds:
                        continue

                checked += 1
                result = await self._cli.get_feed_comments(
                    feed_id,
                    guild_id=guild_id,
                    channel_id=self._channel_id,
                    count=20,
                    rank_type=2,
                    reply_list_num=self._comment_reply_list_num,
                )
                if not result.ok:
                    logger.debug(
                        f"评论轮询：拉取帖子 {feed_id}（频道 {guild_id}）的评论失败：{result.error}"
                    )
                    continue

                feed_title = _as_text(feed.get("title"))
                names = self._nickname_candidates()
                for comment in extract_comments(result.data):
                    notice = notice_from_comment(
                        comment,
                        feed_id=feed_id,
                        guild_id=guild_id,
                        feed_title=feed_title,
                        self_tiny_id=self._self_tiny_id,
                        self_names=names,
                    )
                    if not notice.get("id"):
                        continue
                    collected.append(notice)
                    parent_id = _as_text(notice.get("comment_id"))
                    # 父评论作者 = 这条一级评论的作者：楼中楼回复挂在「我的」评论下时，
                    # 才算在对机器人说话（否则只作频道对话注入，见 notice_from_comment）
                    parent_author = _as_text(comment.get("author_id")) or _as_text(comment.get("authorId"))
                    for reply in extract_replies(comment):
                        reply_notice = notice_from_comment(
                            reply,
                            feed_id=feed_id,
                            guild_id=guild_id,
                            feed_title=feed_title,
                            self_tiny_id=self._self_tiny_id,
                            kind="reply",
                            parent_comment_id=parent_id,
                            parent_author_id=parent_author,
                            self_names=names,
                        )
                        if reply_notice.get("id"):
                            collected.append(reply_notice)

        # 跳过自己发的（避免自问自答）与过期内容
        candidates: list[dict[str, Any]] = []
        for notice in collected:
            author_id = _as_text(notice.get("from_user_id"))
            if self._comment_skip_self and self._self_tiny_id and author_id == self._self_tiny_id:
                continue
            if max_age_seconds > 0:
                try:
                    created = float(notice.get("create_time_raw") or 0)
                except (TypeError, ValueError):
                    created = 0.0
                if created > 0 and now_ts - created > max_age_seconds:
                    continue
            candidates.append(notice)

        fresh = self._watermark.filter_new(candidates)
        if baseline:
            logger.info(f"评论区基线：记下 {len(fresh)} 条（默认不注入）")
            return
        if not fresh:
            if checked and self._log_summary:
                logger.debug(f"评论轮询：检查 {checked} 条帖子，无新评论")
            return

        actionable = fresh[-self._max_notices :]
        logger.info(
            f"评论轮询：检查 {checked} 条帖子，新增 {len(fresh)} 条，注入 {len(actionable)} 条"
        )
        for notice in actionable:
            await self._inject(notice, source="comment")

    async def poll_now(self) -> dict[str, Any]:
        """立即拉取一轮（排障/手动触发用，不影响轮询任务状态）。"""
        before = self._watermark.seen_count
        ok = await self._poll_once()
        return {"ok": ok, "seen_before": before, "seen_after": self._watermark.seen_count}

    def _is_cross_path_duplicate(self, notice: Mapping[str, Any], source: str) -> bool:
        """同一条内容若已由**另一条**路径投递过，则跳过（防"一条消息收两次"）。

        为什么需要它：``feed get-notices`` 的真实返回里**没有** comment_id / reply_id，
        所以通知路径算出的键（合成 id）与评论轮询路径的键（``reply|<feed>|<id>``）
        天然不同，水位线层面的去重拦不住；这里用「帖子 + 规范化正文」兜底，
        且只在**跨路径**时生效（同一路径内重复正文仍按原样放行）。
        """
        fingerprint = _content_fingerprint(notice)
        if fingerprint is None:
            return False
        now = time.monotonic()
        expired = [
            key
            for key, (_, recorded_at) in self._recent_content.items()
            if now - recorded_at > _CONTENT_DEDUP_TTL_SECONDS
        ]
        for key in expired:
            self._recent_content.pop(key, None)
        previous = self._recent_content.get(fingerprint)
        if previous is not None and previous[0] != source:
            logger.info(
                f"跳过跨路径重复内容（{previous[0]} 已投过 → {source}）：{fingerprint[2][:40]}"
            )
            return True
        self._recent_content[fingerprint] = (source, now)
        while len(self._recent_content) > _CONTENT_DEDUP_MAX:
            self._recent_content.pop(next(iter(self._recent_content)), None)
        return False

    async def _inject(self, notice: Mapping[str, Any], *, source: str = "notice") -> None:
        """把一条归一化通知注入统一消息管线。

        ``source``：``notice``（互动通知）/ ``feed``（新帖轮询）/ ``comment``（评论轮询）。
        """
        if self.core_sink is None:
            logger.warning(f"core_sink 尚未就绪，丢弃通知 {notice.get('id')}")
            return
        if self._is_cross_path_duplicate(notice, source):
            return
        # 通知里通常没有发送者昵称（渲染出来是「未知用户」），注入前用详情补一次
        notice = await self._with_sender_nickname(notice)
        # 自己发的评论/回复也会产生「收到评论/回复了我」通知（例如 bot 在自己帖子下留言）：
        # 补昵称时已经回查到作者，这里把它挡掉，避免自问自答。
        if (
            self._comment_skip_self
            and self._self_tiny_id
            and _as_text(notice.get("from_user_id")) == self._self_tiny_id
        ):
            logger.debug(
                f"跳过本账号自己产生的通知（{notice.get('type_label')}，{notice.get('id')}）"
            )
            return
        envelope = self._envelope_for(notice)
        try:
            await self.core_sink.send(envelope)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"注入通知失败：{exc}", exc_info=True)
            return
        info = envelope.get("message_info") or {}
        group_info = info.get("group_info") or {}
        label = group_info.get("group_name") or (info.get("user_info") or {}).get("user_id") or "?"
        logger.info(f"已注入通知：{notice.get('type_label')} → 会话 {label}")

    # ── 发送者昵称补齐 ──────────────────────────────────────

    async def _feed_detail_cached(self, feed_id: str, guild_id: str = "") -> Mapping[str, Any]:
        """取帖子详情，带 TTL 缓存（同一帖子的多条通知不重复调 CLI）。

        多频道下缓存键要带频道：同一个 feed_id 只会出现在它所属的频道里，
        但缓存键带 guild 更安全，也便于排障。
        """
        if not feed_id:
            return {}
        target_guild = guild_id or self._guild_id
        cache_key = f"{target_guild}|{feed_id}"
        now = time.monotonic()
        cached = self._detail_cache.get(cache_key)
        if cached is not None and now - cached[0] < _DETAIL_TTL_SECONDS:
            return cached[1]

        detail: Mapping[str, Any] = {}
        try:
            result = await self._cli.get_feed_detail(
                feed_id, guild_id=target_guild, channel_id=self._channel_id
            )
            if result.ok and isinstance(result.data, Mapping):
                detail = result.data
            elif not result.ok:
                logger.debug(f"补昵称时拉帖子详情失败：{result.error}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"补昵称时拉帖子详情异常：{exc}")

        self._detail_cache[cache_key] = (now, detail)
        while len(self._detail_cache) > _DETAIL_CACHE_MAX:
            self._detail_cache.pop(next(iter(self._detail_cache)), None)
        return detail

    async def _with_sender_nickname(self, notice: Mapping[str, Any]) -> Mapping[str, Any]:
        """注入前补齐「发送者昵称」与可用的评论定位信息。

        平台通知只给 ``summary``（形如「评论了我的帖子:正文」），**不带 comment_id / 作者**，
        所以按类别分开处理：

        - **评论级**（``comment`` / ``reply`` / ``at``）：拿正文去帖子的评论里回查，
          命中唯一作者才采用，并顺手补上 ``comment_id`` / ``comment_author_id`` /
          ``comment_create_time``（这样出站还能走楼中楼）。
          **绝不回退到帖子作者** —— 帖子是 bot 自己发的时候，那会把评论者显示成 bot 自己
          （真实事故：某位成员的评论被渲染成「示例Bot：评论了我的帖子…」）。
        - **帖子级**（``feed`` 新帖）：发送者就是帖子作者。

        任何失败都只记日志、不影响注入（昵称缺失时渲染成「未知用户」）。
        """
        current_nick = _as_text(notice.get("from_nickname"))
        from_user_id = _as_text(notice.get("from_user_id"))
        if current_nick and not current_nick.isdigit() and current_nick != from_user_id:
            return notice  # 通知自带可用昵称
        feed_id = _as_text(notice.get("feed_id"))
        if not feed_id:
            return notice

        target_guild = _as_text(notice.get("guild_id")) or self._guild_id
        detail = await self._feed_detail_cached(feed_id, target_guild)
        enriched: dict[str, Any] = {}
        #: 通知的正文（``normalize_notice`` 已剥掉「评论了我的帖子:」这类前缀）——
        #: 评论级通知没有 comment_id，只能拿它去评论区回查是谁发的。
        notice_content = _as_text(notice.get("content"))

        category = _as_text(notice.get("category")).lower()
        comment_level = category in ("comment", "reply", "at")
        comment_id = _as_text(notice.get("comment_id"))
        if comment_id or comment_level:
            try:
                comments = await self._cli.get_feed_comments(
                    feed_id, guild_id=target_guild, channel_id=self._channel_id, count=20
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"补昵称时拉评论失败：{exc}")
                comments = None
            if comments is not None and getattr(comments, "ok", False):
                match: Mapping[str, Any] | None = None
                if comment_id:
                    match = find_comment_payload(comments.data, comment_id)
                if match is None and comment_level:
                    # 通知不带 comment_id：用正文在评论（含楼中楼）里回查唯一作者
                    match = match_comment_by_body(extract_comments(comments.data), notice_content)
                if match is not None:
                    enriched["from_nickname"] = find_detail_nickname(match)
                    author_id = find_detail_author_id(match) or _as_text(match.get("author_id"))
                    enriched["comment_author_id"] = author_id
                    if author_id:
                        # 关键：把评论者写进 from_user_id —— 会话路由、去重指纹、
                        # 以及「跳过自己产生的通知」都靠它（原来这里是空的，才被帖子作者顶替）
                        enriched["from_user_id"] = author_id
                    matched_id = _as_text(
                        find_field(match, ("comment_id", "commentId", "reply_id", "replyId"))
                    )
                    if matched_id and not comment_id:
                        enriched["comment_id"] = matched_id
                    create_raw = _as_text(
                        find_field(match, ("create_time_raw", "createTimeRaw", "create_time"))
                    )
                    if create_raw.isdigit():
                        enriched["comment_create_time"] = create_raw

        # 只有**帖子级**通知才用帖子作者兜底；评论级宁可留空也不认错人
        if not enriched.get("from_nickname") and not comment_level:
            enriched["from_nickname"] = find_detail_nickname(detail)
        if not _as_text(notice.get("feed_author_id")):
            enriched["feed_author_id"] = find_detail_author_id(detail)

        merged = {**notice, **{k: v for k, v in enriched.items() if v}}
        if merged.get("from_nickname") != notice.get("from_nickname"):
            logger.debug(
                f"已补齐通知发送者昵称：{notice.get('from_nickname')!r} → {merged.get('from_nickname')!r}"
                + (f"（正文回查命中，评论 {merged.get('comment_id')}）" if enriched.get("comment_id") else "")
            )
        elif comment_level and not merged.get("from_nickname"):
            logger.debug(
                f"通知无 comment_id 且正文回查未命中唯一作者，昵称留空"
                f"（feed={feed_id}，正文前 20 字={notice_content[:20]!r}）"
            )
        return merged

    # ── 消息转换（框架契约）────────────────────────────────

    async def from_platform_message(self, raw: Any) -> dict[str, Any]:
        """平台原始通知 → 统一 ``MessageEnvelope``（dict 形态）。"""
        if isinstance(raw, Mapping) and raw.get("category"):
            notice: dict[str, Any] = dict(raw)
        else:
            notice = normalize_notice(
                raw if isinstance(raw, Mapping) else {}, self_names=self._nickname_candidates()
            )
        return self._envelope_for(notice)

    def _envelope_for(self, notice: Mapping[str, Any]) -> dict[str, Any]:
        """构造 envelope 并登记出站上下文。"""
        route = route_for_notice(
            notice,
            platform=self.platform,
            guild_id_fallback=_as_text(notice.get("guild_id")) or self._guild_id,
            channel_as_group=self._channel_as_group,
        )
        self._remember_route(route)
        return envelope_for_notice(notice, route, platform=self.platform)

    def _remember_route(self, route: NoticeRoute) -> None:
        """登记路由上下文（有界，防内存无限增长）并追加待回复目标。"""
        ctx = dict(route.ctx)
        ctx["route_key"] = route.key
        ctx["route_kind"] = route.kind
        self._ctx[route.key] = ctx
        if route.stream_id:
            self._ctx_by_stream[route.stream_id] = ctx
        # 只有「能回复的目标」才进入待回复队列（帖子/评论/私信）
        if ctx.get("feed_id") or route.kind == "private":
            queue = self._pending_targets.setdefault(route.key, [])
            queue.append(ctx)
            while len(queue) > _PENDING_TARGET_MAX:
                queue.pop(0)
        while len(self._ctx) > 500:
            self._ctx.pop(next(iter(self._ctx)), None)
        while len(self._ctx_by_stream) > 500:
            self._ctx_by_stream.pop(next(iter(self._ctx_by_stream)), None)
        while len(self._pending_targets) > 200:
            self._pending_targets.pop(next(iter(self._pending_targets)), None)

    def _take_reply_target(self, key: str) -> dict[str, Any] | None:
        """按配置策略从待回复队列取一条出站目标；队列空则返回 None（宁可不发也不发错位置）。"""
        queue = self._pending_targets.get(key)
        if not queue:
            return None
        if self._reply_target_policy == "oldest":
            return queue.pop(0)
        return queue.pop()

    def context_for(self, key: str) -> dict[str, Any] | None:
        """按路由键或 stream_id 查上下文（排障/其他组件复用）。"""
        return self._ctx.get(key) or self._ctx_by_stream.get(key)

    # ── 出站（框架契约）────────────────────────────────────

    async def _send_platform_message(self, envelope: Mapping[str, Any]) -> None:
        """把 Chatter 的回复发回腾讯频道。"""
        info = envelope.get("message_info") or {}
        message_id = str(info.get("message_id") or "")
        if message_id and self._is_duplicate_out(message_id):
            logger.debug(f"跳过重复出站消息（message_id={message_id}）")
            return

        segment = envelope.get("message_segment")
        if isinstance(segment, Mapping) and str(segment.get("type")) == "adapter_command":
            await self._handle_adapter_command(segment)
            return

        text = extract_text(envelope)
        if not text.strip():
            logger.warning(f"出站消息没有文本内容，已忽略（message_id={message_id or '?'}）")
            return
        if not self._enable_reply:
            logger.info("reply.enabled=false：不发送出站消息")
            return

        kind, key, _parsed = extract_target(info)
        # 频道级会话里同一条流有多个候选落点（多条帖子/评论），按策略从待回复队列取一条
        ctx = self._take_reply_target(key)
        if ctx is None:
            # 队列空是**常态**而非异常：一个回合里 LLM 可能连发多条（多角色 bot 各说一句），
            # 而一条通知只登记一个目标。这时回退到「本会话最近一次成功用过的目标」——
            # 同一个会话键、确定的目标，比丢弃更符合预期（仍是宁可不发也不发错位置的反面极端：
            # 这里不会发到别的会话/别的帖子）。
            ctx = self._last_target.get(key)
            if ctx is not None:
                logger.warning(
                    f"待回复队列为空（kind={kind} key={key}）：回退到本会话最近一次的目标"
                    f"（帖子 {ctx.get('feed_id') or '（私信）'}）—— 一回合多条消息时会这样"
                )
        if ctx is None:
            logger.error(
                f"没有待回复的出站目标（kind={kind} key={key}）：为安全起见不发送，"
                "避免把回复发到错误的帖子/用户"
            )
            return

        text = truncate(text, self._max_length)
        if kind == "group":
            outcome = await send_comment_reply(
                self._cli,
                ctx=ctx,
                text=text,
                guild_id=str(ctx.get("guild_id") or self._guild_id),
                channel_id=self._channel_id,
                self_tiny_id=self._self_tiny_id,
                reply_to_comment=self._reply_to_comment,
                enrich=self._enrich,
            )
        elif kind == "private":
            outcome = await send_dm_reply(
                self._cli,
                ctx=ctx,
                text=text,
                source_guild_id=self._dm_source_guild_id,
                peer_tiny_id=str(ctx.get("from_user_id") or key),
            )
        else:
            logger.error(f"无法识别的出站目标类型（key={key}），已忽略")
            return

        if outcome.ok:
            # 记下「本会话最近一次成功用过的目标」：队列空时回退用它（一回合多条消息的场景）
            self._last_target[key] = dict(ctx)
            while len(self._last_target) > 200:
                self._last_target.pop(next(iter(self._last_target)), None)
            logger.info(f"自动回复成功：{outcome.message}")
        else:
            hint = outcome.result.hint() if outcome.result is not None else ""
            logger.error(f"自动回复失败：{outcome.message}（{hint}）")

    async def _handle_adapter_command(self, segment: Mapping[str, Any]) -> None:
        """``adapter_command`` 控制面：本插件未实现，回错误响应避免调用方超时。"""
        data = segment.get("data")
        data = data if isinstance(data, Mapping) else {}
        request_id = str(data.get("request_id") or "")
        logger.warning(
            f"收到 adapter_command（action={data.get('action')}），本插件未实现适配器命令，已回错误响应"
        )
        if not request_id or self.core_sink is None:
            return
        try:
            await self.core_sink.send(
                {
                    "direction": "incoming",
                    "message_info": {
                        "platform": self.platform,
                        "message_id": request_id,
                        "time": time.time(),
                    },
                    "message_segment": {
                        "type": "adapter_response",
                        "data": {
                            "request_id": request_id,
                            "response": {
                                "status": "error",
                                "message": "tencent_channel 适配器未实现适配器命令",
                            },
                        },
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"回传 adapter_response 失败：{exc}")

    def _is_duplicate_out(self, message_id: str) -> bool:
        """出站去重。

        出站可能同时经 ``MessageSender`` 与 CoreSink 的 outgoing 回调到达
        （两者都指向 ``_send_platform_message``），这里用 message_id 做幂等保护，
        避免同一条回复被发送两次。窗口 120 秒。
        """
        now = time.monotonic()
        for key in [k for k, ts in self._recent_out.items() if now - ts > 120.0]:
            self._recent_out.pop(key, None)
        if message_id in self._recent_out:
            return True
        self._recent_out[message_id] = now
        while len(self._recent_out) > 200:
            self._recent_out.pop(next(iter(self._recent_out)), None)
        return False

    # ── 健康检查（必须重写，避免无 transport 时反复 stop/start）──

    def is_connected(self) -> bool:
        """轮询任务存活且 CLI 可用。"""
        return bool(self._polling and self._cli_ok)

    async def health_check(self) -> bool:
        """健康检查：已暂停（需人工处理）时返回 True，避免触发无意义的重连抖动。"""
        if self._paused_reason:
            logger.debug(f"轮询处于暂停状态（{self._paused_reason}），跳过健康检查重连")
            return True
        if not self._enabled or not self._guilds:
            return True
        return self.is_connected()

    async def reconnect(self) -> None:
        """只重启轮询任务，不调用基类的 stop/start（那会拆掉适配器本体）。"""
        if not self._enabled or not self._guilds or self._paused_reason:
            return
        if self._polling:
            return
        logger.warning("通知轮询任务已停止，正在重启")
        await self._stop_poll_task()
        self._start_poll_task()

    # ── Bot 信息（框架契约）────────────────────────────────

    async def get_bot_info(self) -> dict[str, Any]:
        """返回 Bot 信息（``bot_name`` 与 ``bot_nickname`` 都给，兼容不同消费方）。

        ``bot_id`` 优先用显式配置，其次用 ``channel.bot_qq``（QQ 号是人工填的、最稳定），
        再次用登录账号的 tiny_id，最后占位值。
        """
        bot_id = self._bot_id or self._bot_qq or self._self_tiny_id or "tencent_channel_bot"
        return {
            "bot_id": bot_id,
            "bot_name": self._bot_name,
            "bot_nickname": self._bot_name,
            "platform": self.platform,
        }

    # ── 辅助 ────────────────────────────────────────────────

    @staticmethod
    def _plain(result: CliResult) -> str:
        if isinstance(result.data, Mapping):
            text = result.data.get("text")
            if text:
                return str(text)[:200]
        return json.dumps(result.data, ensure_ascii=False, default=str)[:200] if result.data is not None else ""

    def _probe_self_tiny_id(self, result: CliResult) -> str:
        """从 login status / get-user-info 返回里探测本账号 tiny_id（探测失败不影响运行）。

        注意**不要**把 ``uin`` 当候选：那是 QQ 号，和 tiny_id 不是一个东西
        （接口通常也不返回 QQ 号，所以 ``channel.bot_qq`` 只用于 bot_id 与日志）。
        """
        from .notice_mapping import find_field

        for holder in (result.raw, result.data):
            if not isinstance(holder, Mapping):
                continue
            value = find_field(
                holder,
                ("tiny_id", "tinyId", "tinyid", "tinyID", "member_tinyid", "memberTinyid", "user_id", "userId"),
            )
            text = str(value).strip() if value is not None else ""
            if text.isdigit() and len(text) >= 8:
                return text
        return ""

    def _probe_nickname(self, result: CliResult) -> str:
        """从 get-user-info / login status 返回里取本账号昵称（用于按昵称反查 tiny_id）。"""
        from .notice_mapping import find_field

        for holder in (result.raw, result.data):
            if not isinstance(holder, Mapping):
                continue
            value = find_field(holder, ("global_nickname", "nickname", "nick_name", "nickName"))
            text = str(value).strip() if value is not None else ""
            if text:
                return text
        return ""

    def _probe_member_name(self, result: CliResult) -> str:
        """从 get-user-info 返回里取「频道昵称」（``member_name``，可能与全局昵称不同）。"""
        from .notice_mapping import find_field

        for holder in (result.raw, result.data):
            if not isinstance(holder, Mapping):
                continue
            value = find_field(holder, ("member_name", "memberName", "guild_nick", "guildNick"))
            text = str(value).strip() if value is not None else ""
            if text:
                return text
        return ""

    def _nickname_candidates(self) -> list[str]:
        """探测 tiny_id 时依次尝试的昵称（去重保序）。

        频道昵称与全局昵称可能不同（``member_name`` / ``nickname`` vs ``global_nickname``），
        所以把接口给到的昵称都列上，最后补配置里的 ``channel.bot_name`` ——
        这样「只填一个昵称」也能定位到本账号，不必人工填 tiny_id。
        """
        names: list[str] = []
        for name in (self._self_nickname, self._self_member_name, self._bot_name):
            text = str(name or "").strip()
            if text and text not in names:
                names.append(text)
        return names

    @staticmethod
    def _member_tiny_ids(payload: Any, nickname: str) -> set[str]:
        """从成员搜索结果里取「昵称完全等于 nickname」的所有 tiny_id。

        CLI 的成员结构形如 ``{"nickname": "...", "tinyid": "123456789012345601"}``
        （注意键名是全小写 ``tinyid``），这里对几种常见写法都兼容。
        """
        wanted = str(nickname or "").strip()
        found: set[str] = set()
        if not wanted or not isinstance(payload, (Mapping, list, tuple)):
            return found

        def walk(node: Any, depth: int) -> None:
            if depth > 6:
                return
            if isinstance(node, Mapping):
                tiny_id = ""
                for key in ("tinyid", "tiny_id", "tinyId", "tinyID", "member_tinyid", "memberTinyid"):
                    value = node.get(key)
                    if isinstance(value, bool) or value is None:
                        continue
                    if isinstance(value, (str, int)):
                        text = str(value).strip()
                        if text.isdigit() and len(text) >= 8:
                            tiny_id = text
                            break
                if tiny_id:
                    for key in ("nickname", "nick_name", "nickName", "name", "global_nickname"):
                        value = node.get(key)
                        if isinstance(value, str) and value.strip() == wanted:
                            found.add(tiny_id)
                            break
                for value in node.values():
                    if isinstance(value, (Mapping, list, tuple)):
                        walk(value, depth + 1)
            else:
                for item in list(node)[:50]:
                    walk(item, depth + 1)

        walk(payload, 0)
        return found

    async def _probe_tiny_id_from_members(self) -> str:
        """用本账号昵称在目标频道里搜成员，反查 tiny_id。

        候选昵称按可信度依次尝试（接口返回的昵称/频道昵称 → 配置的 ``channel.bot_name``），
        只在「同名成员唯一」时采用，避免同名误判；都拿不到就返回空（回复降级为帖内评论）。
        这样最小配置只需填昵称，不必人工填 tiny_id。
        """
        candidates = self._nickname_candidates()
        if not candidates or not self._guild_id:
            return ""
        ambiguous: list[str] = []
        for nickname in candidates:
            result = await self._cli.search_members(nickname, guild_id=self._guild_id, num=20)
            if not result.ok:
                logger.debug(f"按昵称「{nickname}」搜成员失败，暂不启用楼中楼回复：{result.error}")
                continue
            tiny_ids: set[str] = set()
            for holder in (result.data, result.raw):
                tiny_ids |= self._member_tiny_ids(holder, nickname)
            if len(tiny_ids) == 1:
                tiny_id = next(iter(tiny_ids))
                logger.info(
                    f"已按昵称「{nickname}」探测到本账号 tiny_id={tiny_id}"
                    "（do-reply 的 replier_id；若这个账号不是本实例该用的那个，检查 cli.login 的令牌）"
                )
                return tiny_id
            if len(tiny_ids) > 1:
                ambiguous.append(f"{nickname}（{len(tiny_ids)} 个同名）")
        if ambiguous:
            logger.warning(
                f"频道里有多个同名成员（{'、'.join(ambiguous)}），无法确定本账号 tiny_id；"
                "可在 channel.self_tiny_id 显式指定"
            )
        else:
            logger.debug(f"频道成员里没搜到这些昵称：{candidates}（可在 channel.bot_name 填频道里显示的名字）")
        return ""

    def _note_hints(self, result: CliResult) -> None:
        """记录 CLI 的 setup_hint / subscribe_hint（只提示，绝不自动开启通知）。"""
        for holder in (result.raw, result.data):
            if not isinstance(holder, Mapping):
                continue
            for field_name in ("setup_hint", "subscribe_hint"):
                hint = holder.get(field_name)
                if not isinstance(hint, Mapping):
                    continue
                message = str(hint.get("message") or "")
                command = str(hint.get("command") or "")
                self._last_hint = message
                logger.warning(
                    f"CLI 提示（{field_name}）：{message}\n如需启用请在终端手动执行："
                    f"{command or 'tencent-channel-cli.cmd manage notices-on --session-key <sessionKey> --confirm'}"
                    "（非 OpenClaw 环境无法自动推送，本插件走 feed get-notices 主动轮询，不需要 notices-on）"
                )

    # ── 水位线持久化（可选）────────────────────────────────

    def _load_state(self) -> None:
        path = self._state_path
        if path is None:
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.info(f"水位线文件不存在（{path}）：本轮按首次轮询处理")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取水位线文件失败（{path}）：{exc}")
            return
        self._watermark = NoticeWatermark.from_dict(payload if isinstance(payload, Mapping) else None)
        self._first_poll = self._watermark.seen_count == 0 and self._watermark.watermark == 0.0
        logger.info(
            f"已恢复通知水位线：已见 {self._watermark.seen_count} 条，水位线 {self._watermark.watermark}"
        )

    def _save_state(self) -> None:
        path = self._state_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._watermark.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"写入水位线文件失败（{path}）：{exc}")
