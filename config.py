"""腾讯频道插件配置。

配置路径（框架约定）：``config/plugins/tencent_channel/config.toml``，
在本机为 ``C:\\Users\\<用户名>\\AppData\\Local\\MoFox Code\\config\\plugins\\tencent_channel\\config.toml``。
配置缺省时框架会按这里的默认值自动生成文件。

可读性约定（**重要**）
----------------------
框架每次加载配置都会用本文件的模型签名**重新渲染** ``config.toml``
（``src.kernel.config.core._render_toml_with_signature``：每个字段输出
「描述注释 + 值类型/默认值注释 + 键值 + 空行」）。因此：

- **不要手改 config.toml 的注释/排版**，下次启动就会被覆盖；要改说明请改这里；
- 分组靠**嵌套配置节**（``[channel.notices]`` / ``[channel.feeds]`` / ``[channel.comments]`` /
  ``[cli.login]``）：节的 docstring 会渲染成分组标题注释；
- 嵌套节的 ``@config_section(...)`` **只能写叶子名**（``"notices"``，**不能**写 ``"channel.notices"``）：
  TOML 表名由渲染器按「父表名 + **字段名**」拼出来，而 WebUI 的 schema 提取
  （``neo-mofox-webui/utils/config_parser.py`` 的 ``_extract_section_recursive``）
  会把父节名**再前缀一次** —— 写成全名会让编辑器拼出 ``channel.channel.notices``，
  保存时报 ``Extra inputs are not permitted``（``channel.channel``）；顶层节名仍写完整名；
- 字段 ``description`` 保持**一句话**（它会被写成一行注释，太长就难扫）；
  详细解释放 ``hint``（只给 WebUI，不进 TOML）；
- 字段名在节内**去掉重复前缀**（``[channel.feeds] enabled`` 而不是 ``watch_new_feeds``）；
- ``input_type`` 只能用 WebUI 前端控件表里的值：``text`` / ``textarea`` / ``number`` / ``slider`` /
  ``switch`` / ``select`` / ``multiselect`` / ``list`` / ``dict`` / ``object`` / ``json`` /
  ``password`` / ``email`` / ``url`` / ``boolean``。**不要用 ``"file"``**：前端 ``de()`` 没有这个分支，
  会兜底成 TextField，而 TextField 把 ``input_type`` 直接写进 ``<input type=...>`` ——
  结果渲染成浏览器的「选择文件」控件（路径无法回显、选中文件还会把 FileList 写进配置）；
  路径类字段请用 ``"text"`` 或 ``"password"``；
- **同一件事只留一个字段**：监听频道统一写 ``[channel] guilds``（列表 / 逗号分隔字符串 /
  单个频道号都行），不再有 ``guild_id`` 与 ``guild_number`` 之分（纯数字按真实 ID、其余按频道号解析）。

旧版（扁平 ``[channel]`` / ``[cli]`` 键、以及 ``guild_id`` / ``guild_number`` 双写法）会在加载时
**自动迁移**到新布局，并打一条 warning；迁移表见模块级 ``_LEGACY_KEYS`` 与 ``_merge_legacy_guilds``。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, ClassVar

import tomllib

from ._compat import BaseConfig, Field, SectionBase, config_section
from .capabilities import (
    DEFAULT_COMPONENT_PROMPTS,
    DEFAULT_READ_TOOL_PROMPT,
    DEFAULT_WRITE_ACTION_PROMPT,
)
from .guild_resolver import split_guild_refs

logger = logging.getLogger("tencent_channel.config")


#: 旧版扁平键 → 新版布局。键 = (旧节, 旧字段)，值 = (新节, 新字段)。
#: 升级插件后第一次加载会把旧值搬过去并重渲染 config.toml，避免用户设置被静默丢弃。
_LEGACY_KEYS: dict[tuple[str, str], tuple[str, str]] = {
    # [channel] 拆分：轮询总闸 / 通知轮询 / 新帖轮询 / 评论区轮询
    ("channel", "enabled"): ("channel", "poll"),
    ("channel", "poll_interval"): ("channel.notices", "poll_interval"),
    ("channel", "poll_page_num"): ("channel.notices", "page_num"),
    ("channel", "max_notices_per_poll"): ("channel.notices", "max_per_poll"),
    ("channel", "notice_types"): ("channel.notices", "types"),
    ("channel", "inject_history_on_first_poll"): ("channel.notices", "inject_history"),
    ("channel", "watch_new_feeds"): ("channel.feeds", "enabled"),
    ("channel", "feed_poll_interval"): ("channel.feeds", "poll_interval"),
    ("channel", "feed_page_num"): ("channel.feeds", "page_num"),
    ("channel", "feed_skip_self"): ("channel.feeds", "skip_self"),
    ("channel", "watch_comments"): ("channel.comments", "enabled"),
    ("channel", "comment_poll_interval"): ("channel.comments", "poll_interval"),
    ("channel", "comment_page_num"): ("channel.comments", "page_num"),
    ("channel", "comment_reply_list_num"): ("channel.comments", "reply_list_num"),
    ("channel", "comment_max_age_hours"): ("channel.comments", "max_age_hours"),
    ("channel", "comment_skip_self"): ("channel.comments", "skip_self"),
    # 自动回复总闸搬到 [reply]
    ("channel", "enable_reply"): ("reply", "enabled"),
    # [cli] 拆分：登录凭证搬到 [cli.login]
    ("cli", "auto_login_qrcode"): ("cli.login", "auto_qrcode"),
    ("cli", "login_token"): ("cli.login", "token"),
    ("cli", "login_token_file"): ("cli.login", "token_file"),
    ("cli", "login_device_id"): ("cli.login", "device_id"),
    ("cli", "login_capture_keychain"): ("cli.login", "capture_keychain"),
    ("cli", "login_wait_seconds"): ("cli.login", "wait_seconds"),
    ("cli", "login_poll_interval"): ("cli.login", "poll_interval"),
    ("cli", "logout_on_startup"): ("cli.login", "logout_on_startup"),
    ("cli", "logout_on_shutdown"): ("cli.login", "logout_on_shutdown"),
    # [log] 去掉冗余的 log_ 前缀
    ("log", "log_raw_notices"): ("log", "raw_notices"),
    ("log", "log_poll_summary"): ("log", "poll_summary"),
}


def _place(target: dict[str, Any], section: str, key: str, value: Any) -> None:
    """把值写进（可能是嵌套的）节字典，不覆盖已有值。"""
    node = target
    for part in section.split("."):
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node.setdefault(key, value)


def _move_legacy_keys(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把旧布局的键搬到新布局，返回 ``(新数据, 迁移说明)``。

    新布局里已有显式值的键不覆盖（以用户当前文件的新写法为准）。
    """
    migrated = {key: value for key, value in raw.items()}
    moves: list[str] = []
    for (old_section, old_key), (new_section, new_key) in _LEGACY_KEYS.items():
        old_node = migrated.get(old_section)
        if not isinstance(old_node, dict) or old_key not in old_node:
            continue
        value = old_node.pop(old_key)
        _place(migrated, new_section, new_key, value)
        moves.append(f"[{old_section}] {old_key} → [{new_section}] {new_key}")
        if not old_node:
            migrated.pop(old_section, None)
    return migrated, moves


#: 旧版「监听频道」的三个字段（顺序 = 取值优先级），全部汇入 ``channel.guilds``
_LEGACY_GUILD_KEYS: tuple[str, ...] = ("guilds", "guild_id", "guild_number")


def _merge_legacy_guilds(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """把 ``guild_id`` / ``guild_number`` 并进单一的 ``channel.guilds``。

    只有出现 ``guild_id`` / ``guild_number`` 时才动手 —— 已经是「只有 guilds」的新布局
    保持幂等，不会触发重写与迁移告警。取值顺序沿用旧版优先级：
    ``guilds`` > ``guild_id`` > ``guild_number``，逐项拆分、去重、保序。
    """
    channel = raw.get("channel")
    if not isinstance(channel, dict):
        return raw, []
    legacy = [key for key in ("guild_id", "guild_number") if key in channel]
    if not legacy:
        return raw, []

    refs: list[str] = []
    for key in _LEGACY_GUILD_KEYS:
        if key not in channel:
            continue
        for ref in split_guild_refs(channel.pop(key)):
            if ref not in refs:
                refs.append(ref)
    channel["guilds"] = refs
    return raw, [f"[channel] {key} → [channel] guilds" for key in legacy]


def _migrate_legacy_config(path: Path, *, write: bool) -> dict[str, Any] | None:
    """检测并迁移旧布局配置；无旧键或文件不可读时返回 ``None``（走框架默认流程）。

    Args:
        path: 配置文件路径。
        write: 是否把迁移结果按新签名重渲染回写（``auto_update`` 为 True 时）。
    """
    if not path.is_file():
        return None
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:  # 读不了/语法错：交给框架报它自己的错
        logger.debug("迁移前读取配置失败（跳过迁移）：%s", exc)
        return None

    migrated, moves = _move_legacy_keys(raw)
    migrated, guild_moves = _merge_legacy_guilds(migrated)
    moves.extend(guild_moves)
    if not moves:
        return None

    logger.warning(
        "检测到 %d 个旧版配置键，已自动迁移到新布局（%s%s）；"
        "新文件按插件模型的注释重新渲染，请以 config.py 为准",
        len(moves),
        "；".join(moves[:3]),
        " …" if len(moves) > 3 else "",
    )

    try:  # 与 BaseConfig.generate_default 同款：复用框架的合并 + 渲染
        from src.kernel.config.core import (  # type: ignore[import-not-found]
            _merge_with_model_defaults,
            _render_toml_with_signature,
        )
    except Exception as exc:  # noqa: BLE001 - 框架内部接口变化时只降级，不阻断加载
        logger.warning("框架渲染接口不可用（%s）：本次只迁移内存中的值，文件下次启动再更新", exc)
        return migrated

    merged = _merge_with_model_defaults(TencentChannelConfig, migrated)
    if write:
        try:
            path.write_text(
                _render_toml_with_signature(TencentChannelConfig, merged), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("迁移后回写配置失败（%s）：已按迁移结果运行", exc)
    return merged


class TencentChannelConfig(BaseConfig):
    """腾讯频道（QQ 频道）插件配置。"""

    #: 组件名与描述用当前的 ``name`` / ``description``（``config_name`` / ``config_description``
    #: 是核心的 legacy 别名，工具链会告警）。实测 1.2.0 与 1.3.0-alpha 都以 name/description 为准。
    name: ClassVar[str] = "config"
    description: ClassVar[str] = "腾讯频道（QQ 频道）插件配置"

    # ── [plugin] ────────────────────────────────────────────

    @config_section("plugin", title="插件总开关", tag="plugin")
    class PluginSection(SectionBase):
        """插件总开关：false = 整体停用（不轮询、不登录，工具与动作也不进提示词）。"""

        enabled: bool = Field(
            default=True,
            label="启用插件",
            tag="plugin",
            input_type="switch",
            description="插件总开关；false = 整体停用",
            hint="关掉后连 LLM 的可用工具与动作都会消失，等同于「装了但没启用」",
        )

    # ── [channel] ───────────────────────────────────────────

    @config_section("channel", title="频道与会话", tag="network")
    class ChannelSection(SectionBase):
        """目标频道与会话形态。

        **最小配置只需 3 项**：``guilds``（频道号）、``bot_name``（bot 昵称）、``bot_qq``（bot QQ 号）。
        其余「在 QQ 客户端里看不到」的东西都会自动获取：

        | 项 | 来源 |
        |---|---|
        | 真实频道 ID（guild_id） | 频道号 → ``manage get-my-join-guild-info`` 换算 |
        | 版块 ID（channel_id） | ``manage get-guild-channel-list`` 取「全部」版块 |
        | 本账号 tiny_id | 按昵称在频道里搜成员（同名唯一时采用） |
        | bot_id | 留空时用 ``bot_qq``，其次用登录账号 tiny_id |
        """

        guilds: list[str] | str = Field(
            default_factory=list,
            label="监听频道（必填）",
            tag="list",
            input_type="list",
            item_type="str",
            placeholder="pd20589127",
            description="① 频道号（QQ 客户端里能看到的那串 pd…）",
            hint="也可写真实频道 ID（纯数字）；支持多个：列表、逗号分隔字符串或单个字符串。"
            "第一个解析成功的作为默认频道（工具/动作不显式指定频道时用它）",
        )
        bot_name: str = Field(
            default="QQ频道助手",
            label="bot 昵称（必填）",
            tag="user",
            placeholder="你的 bot 在频道里显示的名字",
            description="② bot 昵称：用于自动探测本账号 tiny_id（楼中楼回复要用）",
            hint="填频道里显示的名字即可；与 QQ 全局昵称不一致也没关系（两个都会拿来试）。"
            "同名多人时会放弃自动探测，这时才需要手工填 self_tiny_id",
        )
        bot_qq: str = Field(
            default="",
            label="bot QQ 号（建议填）",
            tag="user",
            placeholder="123456789",
            description="③ bot 的 QQ 号：用于 bot_id 与日志",
            hint="频道接口不返回 QQ 号，所以它只作标识/日志用途，无法用来反查账号（登录身份由 cli.login 的令牌决定）",
        )
        channel_id: str = Field(
            default="",
            label="版块 ID（留空自动获取）",
            tag="advanced",
            placeholder="留空即可",
            description="版块 ID：留空自动取（优先 section_name，其次「全部」版块）",
            hint="版块 ID 在 QQ 客户端里看不到；通常不用填 —— 想换默认发帖版块请用下面的 section_name（填版块名）",
        )
        section_name: str = Field(
            default="",
            label="默认版块名（留空自动）",
            tag="advanced",
            placeholder="全部",
            description="默认版块名（如「全部」「闲聊」）：留空自动取「全部」版块",
            hint="按**名字**填即可，无需查 ID；与 channel_id 同时填时以 channel_id 为准。"
            "临时发到别的版块：让 bot 调 channel_publish_feed 时传 channel_id=版块名（先用 channel_sections 查）",
        )
        self_tiny_id: str = Field(
            default="",
            label="本账号 tiny_id（留空自动探测）",
            tag="advanced",
            description="本账号 tiny_id（楼中楼 replier_id）：留空按昵称自动探测",
            hint="缺了会从楼中楼降级为帖内评论；同名成员多个时才需要手工指定",
        )
        bot_id: str = Field(
            default="",
            label="Bot ID（留空自动）",
            tag="advanced",
            description="Bot ID：留空用 bot_qq，其次用登录账号 tiny_id",
        )
        poll: bool = Field(
            default=True,
            label="轮询频道",
            tag="network",
            input_type="switch",
            description="是否轮询频道；false = 只提供工具/动作",
            hint="关掉后适配器不拉通知、不拉帖子，但 channel_read / channel_write 等组件仍可用",
        )
        channel_as_group: bool = Field(
            default=False,
            label="频道当群聊",
            tag="general",
            input_type="switch",
            description="整个频道当一个群（所有帖子/评论同一条会话）",
            hint="默认按帖子/评论分会话；打开后出站落点由 reply_target_policy 决定",
        )
        reply_target_policy: str = Field(
            default="latest",
            label="回复落点策略",
            tag="general",
            input_type="select",
            choices=["latest", "oldest"],
            description="频道级会话下回复落在最新/最早一条待回复",
            hint="队列为空时不发送（宁可不发也不发错位置）；非法值回落 latest",
        )
        dm_source_guild_id: str = Field(
            default="",
            label="私信来源频道",
            tag="network",
            description="主动私信的来源频道（留空用主频道，支持频道号）",
        )
        state_path: str = Field(
            default="",
            label="水位线文件",
            tag="file",
            input_type="text",
            placeholder="data/tencent_channel/watermark.json",
            description="水位线持久化文件（留空 = 仅内存）",
            hint="重启后靠它避免重复注入/重复回复；留空则每次重启重建基线（宁可漏也不重复）",
        )

        # ── 子节：三条轮询路径各自成节，便于分组阅读 ──────

        @config_section("notices", title="通知轮询", tag="network")
        class NoticesSection(SectionBase):
            """互动通知轮询（评论 / 回复 / @ / 私信；点赞、收藏、顶帖由 types 过滤）。"""

            enabled: bool = Field(
                default=True,
                label="启用",
                tag="network",
                input_type="switch",
                description="是否轮询互动通知",
            )
            poll_interval: float = Field(
                default=600.0,
                label="轮询间隔（秒）",
                tag="timer",
                input_type="number",
                description="轮询间隔秒：默认 600（10 分钟），可自行调整",
                hint="它同时是**主循环节拍**（每轮都拉一次互动通知）。调小更及时但更费"
                "（每次都要拉起 node / 走一次网关）；代码下限 5 秒",
            )
            page_num: int = Field(
                default=20,
                label="每次拉取条数",
                tag="performance",
                input_type="number",
                description="每次拉取条数（feed get-notices --page-num）",
            )
            max_per_poll: int = Field(
                default=20,
                label="单轮注入上限",
                tag="performance",
                input_type="number",
                description="单轮最多注入条数（超出只取最新 N 条）",
            )
            types: list[str] = Field(
                default_factory=lambda: ["comment", "reply", "at", "dm"],
                label="注入类别",
                tag="list",
                input_type="list",
                item_type="str",
                description="注入哪些类别：comment/reply/at/dm（可加 like/top/favorite）",
            )
            inject_history: bool = Field(
                default=False,
                label="首轮注入历史",
                tag="advanced",
                input_type="switch",
                description="首轮是否注入历史通知（false = 只建立基线）",
                hint="打开会把积压的历史互动一次性喂给 LLM，容易冷启动刷屏",
            )

        @config_section("feeds", title="新帖轮询", tag="network")
        class FeedsSection(SectionBase):
            """新帖轮询（帖子不产生互动通知；想「别人发帖也插话」就打开）。"""

            enabled: bool = Field(
                default=False,
                label="启用",
                tag="network",
                input_type="switch",
                description="是否轮询频道主页新帖",
            )
            poll_interval: float = Field(
                default=600.0,
                label="轮询间隔（秒）",
                tag="timer",
                input_type="number",
                description="新帖轮询间隔秒：默认 600（10 分钟），可自行调整",
                hint="帖子列表变化慢，通常比通知更稀疏（每条帖子一次调用）；代码下限 15 秒",
            )
            page_num: int = Field(
                default=10,
                label="每次拉取条数",
                tag="performance",
                input_type="number",
                description="每次拉取帖子条数（feed get-guild-feeds --count）",
            )
            skip_self: bool = Field(
                default=True,
                label="跳过自己的帖子",
                tag="general",
                input_type="switch",
                description="跳过自己发的帖子，避免机器人评论自己",
            )

        @config_section("comments", title="评论区轮询", tag="network")
        class CommentsSection(SectionBase):
            """评论区轮询（把别人之间的评论/楼中楼也注入，群聊感的关键）。"""

            enabled: bool = Field(
                default=False,
                label="启用",
                tag="network",
                input_type="switch",
                description="是否轮询帖子评论区",
            )
            poll_interval: float = Field(
                default=600.0,
                label="轮询间隔（秒）",
                tag="timer",
                input_type="number",
                description="评论轮询间隔秒：默认 600（10 分钟），可自行调整",
                hint="每条帖子一次调用，间隔越小越费；代码下限 15 秒",
            )
            page_num: int = Field(
                default=5,
                label="参与轮询的帖子数",
                tag="performance",
                input_type="number",
                description="最新多少条帖子参与轮询（每条一次 CLI 调用）",
            )
            reply_list_num: int = Field(
                default=1,
                label="预加载楼中楼数",
                tag="performance",
                input_type="number",
                description="每条评论预加载的楼中楼数（0–10）",
            )
            max_age_hours: float = Field(
                default=72.0,
                label="只注入多久内的评论",
                tag="timer",
                input_type="number",
                description="只注入该时间内的评论/回复（0 = 不限）",
                hint="避免重启后翻旧账；无时间戳的条目不受此限制",
            )
            skip_self: bool = Field(
                default=True,
                label="跳过自己的评论",
                tag="general",
                input_type="switch",
                description="跳过本账号自己发的评论/回复，避免自问自答",
            )

        notices: NoticesSection = Field(default_factory=NoticesSection)
        feeds: FeedsSection = Field(default_factory=FeedsSection)
        comments: CommentsSection = Field(default_factory=CommentsSection)

    # ── [cli] ───────────────────────────────────────────────

    @config_section("cli", title="CLI 调用", tag="advanced")
    class CliSection(SectionBase):
        """CLI / 网关调用方式（登录凭证见 [cli.login]）。"""

        mode: str = Field(
            default="auto",
            label="调用方式",
            tag="advanced",
            input_type="select",
            choices=["auto", "node", "cmd", "python", "exe", "gateway"],
            description="auto / node / cmd / python / exe / gateway",
            hint="gateway = 免 CLI 直连 MCP 网关（读 6 + 写 6 条已真机验证，令牌见 [cli.login]）",
        )
        path: str = Field(
            default="tencent-channel-cli.cmd",
            label="CLI 路径",
            tag="file",
            input_type="text",
            description="CLI 路径（Windows 勿用 .ps1，策略受限会卡死）",
            hint="填默认值时插件会自动发现 %APPDATA%\\npm 下的 exe（优先）或 .cmd",
        )
        gateway_endpoint: str = Field(
            default="https://graph.qq.com/mcp_gateway/open_platform_agent_mcp/mcp",
            label="网关端点",
            tag="network",
            description="mode=gateway 的 MCP 端点（一般不用改）",
        )
        node_path: str = Field(
            default="",
            label="node 路径",
            tag="file",
            input_type="text",
            description="node 可执行文件路径（mode=node，留空用 PATH）",
        )
        python_path: str = Field(
            default="",
            label="python 路径",
            tag="file",
            input_type="text",
            description="python 可执行文件路径（mode=python，测试用）",
        )
        timeout: float = Field(
            default=60.0,
            label="调用超时（秒）",
            tag="timer",
            input_type="number",
            description="单次调用超时秒（含 node 启动开销）",
        )
        rate_limit_sleep: float = Field(
            default=70.0,
            label="限流休眠（秒）",
            tag="timer",
            input_type="number",
            description="遇 retCode 153 限流时的休眠秒数（官方要求 70s）",
        )
        dry_run: bool = Field(
            default=False,
            label="演练模式",
            tag="debug",
            input_type="switch",
            description="只构造命令不执行（CLI --dry-run，排障用）",
        )

        @config_section("login", title="登录凭证", tag="security")
        class LoginSection(SectionBase):
            """登录凭证生命周期（启动清凭证 → 令牌回注 → 二维码兜底 → 关机退登）。"""

            token_file: str = Field(
                default="",
                label="令牌文件",
                tag="security",
                input_type="text",
                description="令牌文件（优先于 token；等同登录凭证，勿外发）",
                hint="JSON {token, device_id} / QQ_AI_CONNECT_TOKEN=… 行 / 纯文本；用 export_login_token.py 导出",
            )
            token: str = Field(
                default="",
                label="访问令牌",
                tag="security",
                input_type="password",
                description="访问令牌明文（不推荐，改用 token_file）",
            )
            device_id: str = Field(
                default="",
                label="device-id",
                tag="security",
                description="与令牌配套的 device-id（可选，文件里也可带）",
            )
            capture_keychain: bool = Field(
                default=True,
                label="清凭证前抓取令牌",
                tag="security",
                input_type="switch",
                description="清凭证前先抓本机凭据用于回注（免重复扫码）",
                hint="关掉即恢复「严格一次性会话」语义：每次启动都要重新扫码",
            )
            auto_qrcode: bool = Field(
                default=False,
                label="未登录时打印二维码",
                tag="notification",
                input_type="switch",
                description="未登录时打印授权链接与二维码并等扫码",
            )
            wait_seconds: float = Field(
                default=600.0,
                label="等扫码时长（秒）",
                tag="timer",
                input_type="number",
                description="等扫码时长秒，超时本次不轮询",
            )
            poll_interval: float = Field(
                default=10.0,
                label="扫码轮询间隔（秒）",
                tag="timer",
                input_type="number",
                description="等扫码期间轮询 login poll-token 的间隔秒",
            )
            logout_on_startup: bool = Field(
                default=False,
                label="启动清残留凭证",
                tag="security",
                input_type="switch",
                description="启动时先清残留凭证（强杀环境下唯一可靠点）",
                hint="想「退出即失效」就用它；配 capture_keychain / token_file 可免重复扫码",
            )
            logout_on_shutdown: bool = Field(
                default=False,
                label="关机退登",
                tag="security",
                input_type="switch",
                description="优雅关机（ON_STOP）时退登；强杀/断电不触发",
            )

        login: LoginSection = Field(default_factory=LoginSection)

    # ── [reply] / [log] ─────────────────────────────────────

    @config_section("reply", title="自动回复", tag="general")
    class ReplySection(SectionBase):
        """自动回复策略（出站文本怎么发、发不全时怎么退）。"""

        enabled: bool = Field(
            default=True,
            label="启用自动回复",
            tag="general",
            input_type="switch",
            description="是否自动回复（false = 只注入通知）",
        )
        max_length: int = Field(
            default=1000,
            label="正文上限（字符）",
            tag="text",
            input_type="number",
            description="回复正文上限，超出截断加省略号",
            hint="发帖正文超限会被拒绝而不是截断（短贴 1000 / 长贴 10000 由动作层校验）",
        )
        reply_to_comment: bool = Field(
            default=True,
            label="优先楼中楼",
            tag="general",
            input_type="switch",
            description="评论级通知优先楼中楼（do-reply）",
            hint="字段不全或 do-reply 被拒时自动回退帖内评论，并在结果里说明原因",
        )
        enrich_comment_context: bool = Field(
            default=True,
            label="补齐必填字段",
            tag="advanced",
            input_type="switch",
            description="回复前补齐 feed/comment 必填字段（关掉易 8010）",
        )

    @config_section("log", title="日志", tag="debug")
    class LogSection(SectionBase):
        """日志与排障开关。"""

        raw_notices: bool = Field(
            default=False,
            label="打印原始通知",
            tag="debug",
            input_type="switch",
            description="打印原始通知 JSON（排障用，截断输出）",
        )
        poll_summary: bool = Field(
            default=True,
            label="轮询统计",
            tag="debug",
            input_type="switch",
            description="每轮打印轮询统计摘要",
        )

    # ── [capabilities] ──────────────────────────────────────

    @config_section("capabilities", title="能力开关", tag="advanced")
    class CapabilitiesSection(SectionBase):
        """逐能力开关：默认「只读/写入开，敏感/破坏性关」。

        名单里可写：能力名（feed.del-feed）/ 类别名（read|write|sensitive|danger）/
        域名（feed|manage|cli）/ "*"；disabled 优先级最高。
        完整清单见插件目录 capability_spec.py（由 gen_capability_spec.py 生成）。
        """

        enabled: list[str] = Field(
            default_factory=list,
            label="显式开启",
            tag="list",
            input_type="list",
            item_type="str",
            description='显式开启：能力名 / 类别名 / 域名 / "*"',
        )
        disabled: list[str] = Field(
            default_factory=list,
            label="显式关闭",
            tag="list",
            input_type="list",
            item_type="str",
            description="显式关闭（优先级最高），取值同上",
        )
        read_default: bool = Field(
            default=True,
            label="只读默认开",
            tag="security",
            input_type="switch",
            description="只读能力的默认开关",
        )
        write_default: bool = Field(
            default=True,
            label="写入默认开",
            tag="security",
            input_type="switch",
            description="内容写入（发帖/评论/回复/点赞/私信）默认开关",
        )
        sensitive_default: bool = Field(
            default=False,
            label="敏感默认开",
            tag="security",
            input_type="switch",
            description="敏感能力（改资料/版块/身份组/帖子状态）默认开关",
        )
        danger_default: bool = Field(
            default=False,
            label="破坏性默认开",
            tag="security",
            input_type="switch",
            description="破坏性能力（删除/踢人/禁言/退出）默认开关",
            hint="开启后执行时仍需 confirm=true；升级 CLI 后新增的写命令一律按敏感（默认关）处理",
        )
        expose_generic_read: bool = Field(
            default=True,
            label="暴露通用只读工具",
            tag="advanced",
            input_type="switch",
            description="注册通用只读工具 channel_read",
        )
        expose_generic_write: bool = Field(
            default=True,
            label="暴露通用写入动作",
            tag="advanced",
            input_type="switch",
            description="注册通用写入动作 channel_write",
        )
        allow_yes_for_danger: bool = Field(
            default=False,
            label="破坏性命令加 --yes",
            tag="security",
            input_type="switch",
            description="破坏性命令是否附加 --yes 跳过 CLI 人工确认",
            hint="默认关；关着时 CLI 若要求确认可能因等待输入而超时",
        )

    # ── [prompts] ───────────────────────────────────────────

    @config_section("prompts", title="提示词", tag="text")
    class PromptsSection(SectionBase):
        """LLM 提示词（留空用内置默认；read_tool/write_action 支持 {capabilities} 占位符；改完重启生效）。"""

        read_tool: str = Field(
            default=DEFAULT_READ_TOOL_PROMPT,
            label="通用只读工具提示词",
            tag="text",
            input_type="textarea",
            rows=8,
            description="channel_read 的提示词（{capabilities} = 已开启的只读清单）",
        )
        write_action: str = Field(
            default=DEFAULT_WRITE_ACTION_PROMPT,
            label="通用写入动作提示词",
            tag="text",
            input_type="textarea",
            rows=8,
            description="channel_write 的提示词（{capabilities} = 已开启的写清单）",
        )
        capability_hints: list[str] = Field(
            default_factory=list,
            label="逐能力提示词覆盖",
            tag="list",
            input_type="list",
            item_type="str",
            placeholder="feed.del-feed=删除帖子（不可恢复，慎用）",
            description='逐能力覆盖，元素形如 "feed.del-feed=说明"',
        )
        notices: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["notices"],
            label="channel_notices",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_notices 工具提示词",
        )
        search_feeds: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["search_feeds"],
            label="channel_search_feeds",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_search_feeds 工具提示词",
        )
        feed_detail: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["feed_detail"],
            label="channel_feed_detail",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_feed_detail 工具提示词",
        )
        guild_info: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["guild_info"],
            label="channel_guild_info",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_guild_info 工具提示词",
        )
        members: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["members"],
            label="channel_members",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_members 工具提示词",
        )
        guilds: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["guilds"],
            label="channel_guilds",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_guilds 工具提示词（本账号所在频道列表）",
        )
        sections: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["sections"],
            label="channel_sections",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_sections 工具提示词（频道内版块列表）",
        )
        publish_feed: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["publish_feed"],
            label="channel_publish_feed",
            tag="text",
            input_type="textarea",
            rows=4,
            description="channel_publish_feed 动作提示词",
        )
        comment_feed: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["comment_feed"],
            label="channel_comment_feed",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_comment_feed 动作提示词",
        )
        reply_comment: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["reply_comment"],
            label="channel_reply_comment",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_reply_comment 动作提示词",
        )
        like: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["like"],
            label="channel_like",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_like 动作提示词",
        )
        send_dm: str = Field(
            default=DEFAULT_COMPONENT_PROMPTS["send_dm"],
            label="channel_send_dm",
            tag="text",
            input_type="textarea",
            rows=3,
            description="channel_send_dm 动作提示词",
        )

    # ── 节字段（声明顺序 = TOML 里的出现顺序）───────────────

    plugin: PluginSection = Field(default_factory=PluginSection)
    channel: ChannelSection = Field(default_factory=ChannelSection)
    cli: CliSection = Field(default_factory=CliSection)
    reply: ReplySection = Field(default_factory=ReplySection)
    log: LogSection = Field(default_factory=LogSection)
    capabilities: CapabilitiesSection = Field(default_factory=CapabilitiesSection)
    prompts: PromptsSection = Field(default_factory=PromptsSection)

    # ── 旧布局迁移 ──────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path, *, auto_update: bool = False):
        """加载配置；若文件还是旧布局，先迁移再交给框架渲染。

        ``BaseConfig.load`` 会在签名不一致时把文件重渲染成新布局，但**不会**把
        旧键的值搬过去（未知键会被静默丢弃）。这里补上这一步，避免升级插件后
        用户的频道、轮询、凭证设置在下次启动时悄悄变回默认值。
        """
        migrated = _migrate_legacy_config(Path(path), write=auto_update)
        if migrated is None:
            return super().load(path, auto_update=auto_update)
        return cls.from_dict(migrated)


__all__ = ["TencentChannelConfig"]
