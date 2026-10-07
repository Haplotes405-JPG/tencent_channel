"""CLI 能力清单、开关判定与提示词装配。

分工：
- ``capability_spec.py``：**由脚本生成**（``tools/gen_capability_spec.py``），只描述
  「tencent-channel-cli 有哪些命令、参数叫什么、官方说明是什么」；
- 本模块：在它之上补上**分类**（read / write / sensitive / danger）、**开关判定**与
  **提示词装配**——这两个都是人工可审、可配置的部分。

配置（``config.toml``）：
- ``[capabilities]``：逐能力开关（``enabled`` / ``disabled`` 名单，支持 ``*`` 与类别名），
  以及四类能力的兜底默认值；``expose_generic_read`` / ``expose_generic_write`` 决定是否
  注册通用只读工具 / 通用写入动作；
- ``[prompts]``：所有工具与动作的**提示词**（留空用内置默认），``read_tool`` /
  ``write_action`` 支持 ``{capabilities}`` 占位符；``capability_hints`` 可以逐能力覆盖
  提示词（元素形如 ``"feed.del-feed=删除帖子（不可恢复）"``）。

**本模块不依赖框架**（只依赖 stdlib + ``capability_spec``），因此可以离线单测。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .capability_spec import CAPABILITIES as _SPEC
from .capability_spec import SPEC_CLI_VERSION, SPEC_GENERATED_AT

# ── 能力分类 ────────────────────────────────────────────────

#: 能力类别（决定默认开关）
KIND_READ = "read"
KIND_WRITE = "write"
KIND_SENSITIVE = "sensitive"
KIND_DANGER = "danger"
KINDS: tuple[str, ...] = (KIND_READ, KIND_WRITE, KIND_SENSITIVE, KIND_DANGER)

#: 被排除的命令：交互式 / 常驻 / 属于运维口令，不交给 LLM
#: （值 = 排除原因，会写进日志与文档）
EXCLUDED: dict[str, str] = {
    # CLI 运维口令：登录态由插件自己管理（开机扫码 / 优雅关机退登），不交给 LLM
    "cli.login": "登录/授权属于运维口令，插件自己管理登录态",
    "cli.login.poll-token": "同上",
    "cli.login.logout": "同上（会把机器人自己的登录凭证清掉）",
    "cli.completion": "生成 shell 补全，与业务无关",
    "cli.help": "CLI 自带帮助，与业务无关",
    "cli.schema": "CLI 自带 schema，与业务无关",
    "cli.logs": "日志查看/清理属于运维口令（清理会删日志）",
    # 交互式命令：需要人工在终端里选择，插件里无法交互
    "feed.quick-publish": "交互式命令（终端里选频道/版块），改用 feed.publish-feed",
    "feed.search-and-comment": "交互式命令，改用 feed.search-guild-feeds + feed.do-comment",
    "feed.delete-and-mute": "交互式 + 破坏性命令，改用 feed.del-feed + manage.modify-member-shut-up",
    "manage.search-and-join": "交互式命令，改用 manage.search-guild-content + manage.join-guild",
    # 常驻进程：会占住调用，不适合插件内同步调用
    "manage.notify-daemon": "后台常驻通知服务，插件自己轮询，不需要它",
}

#: 逐能力分类覆盖（未列出时按官方 group 兜底：read/query→read，其它→sensitive）
KIND_OVERRIDES: dict[str, str] = {
    # ── 只读（默认开）──
    # 由 group 兜底为 read，这里只列需要显式确认的
    # ── 内容写入（默认开）──
    "feed.publish-feed": KIND_WRITE,
    "feed.do-comment": KIND_WRITE,
    "feed.do-reply": KIND_WRITE,
    "feed.do-like": KIND_WRITE,
    "feed.do-feed-prefer": KIND_WRITE,
    "manage.push-group-dm-msg": KIND_WRITE,
    # ── 敏感操作（默认关：改频道配置 / 版块 / 身份组 / 帖子状态 / 通知订阅）──
    "feed.alter-feed": KIND_SENSITIVE,
    "feed.move-feed": KIND_SENSITIVE,
    "feed.top-feed": KIND_SENSITIVE,
    "feed.set-feed-essence": KIND_SENSITIVE,
    "feed.push-essence-feed": KIND_SENSITIVE,
    "manage.notices-on": KIND_SENSITIVE,
    "manage.notices-off": KIND_SENSITIVE,
    "manage.subscribe-notices": KIND_SENSITIVE,
    "manage.unsubscribe-notices": KIND_SENSITIVE,
    "manage.update-guild-info": KIND_SENSITIVE,
    "manage.modify-guild-number": KIND_SENSITIVE,
    "manage.upload-guild-avatar": KIND_SENSITIVE,
    "manage.update-join-guild-setting": KIND_SENSITIVE,
    "manage.create-channel": KIND_SENSITIVE,
    "manage.modify-channel": KIND_SENSITIVE,
    "manage.create-guild-role-group": KIND_SENSITIVE,
    "manage.modify-guild-role-group": KIND_SENSITIVE,
    "manage.add-role-members": KIND_SENSITIVE,
    "manage.remove-role-members": KIND_SENSITIVE,
    "manage.join-guild": KIND_SENSITIVE,
    "manage.create-theme-private-guild": KIND_SENSITIVE,
    "manage.deal-notice": KIND_SENSITIVE,
    # ── 破坏性操作（默认关：删除 / 踢人 / 禁言 / 权限变更 / 退出）──
    "feed.del-feed": KIND_DANGER,
    "manage.delete-channel": KIND_DANGER,
    "manage.kick-guild-member": KIND_DANGER,
    "manage.modify-member-shut-up": KIND_DANGER,
    "manage.add-admin": KIND_DANGER,
    "manage.remove-admin": KIND_DANGER,
    "manage.leave-guild": KIND_DANGER,
}

#: 取值守卫：这些命令里带「删除 / 取消」语义的枚举参数被钉死在安全值上。
#: 键 = 能力名，值 = {参数名: 允许的取值}
SAFE_GUARDS: dict[str, dict[str, tuple[Any, ...]]] = {
    "feed.do-comment": {"comment_type": (1,)},  # 1=发表；0=自删 2=帖主删他人
    "feed.do-reply": {"reply_type": (1,)},  # 1=发表；0=自删 2=帖主删
    "feed.do-like": {"like_type": (3, 4, 5, 6)},  # 只允许「点赞/取消自己的赞」
    "feed.do-feed-prefer": {"action": (1, 3)},  # 1=点赞 3=取消
    "feed.publish-feed": {"feed_type": (1, 2)},  # 1=短贴 2=长贴
}

#: 需要二次确认（``confirm=true``）的能力：危险类 + 会通知全频道的
CONFIRM_REQUIRED_KINDS: tuple[str, ...] = (KIND_DANGER,)


def _resolve_kind(name: str, group: str) -> str:
    """决定一个能力属于哪一类。"""
    override = KIND_OVERRIDES.get(name)
    if override:
        return override
    normalized = (group or "").strip().lower()
    if normalized in ("read", "query"):
        return KIND_READ
    # 未显式分类的写命令一律按「敏感」处理（默认关），避免升级 CLI 后新命令悄悄放开
    return KIND_SENSITIVE


@dataclass(frozen=True)
class Capability:
    """一条 CLI 能力。"""

    name: str  # 形如 feed.publish-feed
    domain: str  # feed / manage / cli
    action: str  # publish-feed
    kind: str  # read / write / sensitive / danger
    summary: str  # 中文说明（官方 short，可被配置覆盖）
    group: str  # 官方分组（read/query/write/shortcut）
    params: tuple[str, ...] = ()
    required: tuple[str, ...] = ()

    @property
    def is_read(self) -> bool:
        return self.kind == KIND_READ


def _build() -> tuple[Capability, ...]:
    result: list[Capability] = []
    for item in _SPEC:
        name = str(item.get("name", "")).strip()
        if not name or name in EXCLUDED:
            continue
        group = str(item.get("group", ""))
        result.append(
            Capability(
                name=name,
                domain=str(item.get("domain", "")),
                action=str(item.get("action", "")),
                kind=_resolve_kind(name, group),
                summary=str(item.get("summary", "")),
                group=group,
                params=tuple(str(p) for p in item.get("params", ()) or ()),
                required=tuple(str(p) for p in item.get("required", ()) or ()),
            )
        )
    return tuple(result)


#: 全部可用能力（已剔除交互式/运维口令）
CAPABILITIES: tuple[Capability, ...] = _build()
#: 名字 → 能力
BY_NAME: dict[str, Capability] = {cap.name: cap for cap in CAPABILITIES}
#: 全部能力名（含被排除的，供文档/校验用）
ALL_SPEC_NAMES: tuple[str, ...] = tuple(str(item.get("name", "")) for item in _SPEC)


def capability_by_name(name: str) -> Capability | None:
    """按能力名取能力（形如 ``feed.publish-feed``）；不存在返回 None。"""
    return BY_NAME.get(str(name or "").strip())


def read_capabilities() -> tuple[Capability, ...]:
    """全部只读能力。"""
    return tuple(cap for cap in CAPABILITIES if cap.kind == KIND_READ)


def write_capabilities() -> tuple[Capability, ...]:
    """全部写能力（含敏感与破坏性）。"""
    return tuple(cap for cap in CAPABILITIES if cap.kind != KIND_READ)


# ── 内置默认提示词（只是 config.toml 的种子值，运行时以配置为准）──

DEFAULT_READ_TOOL_PROMPT = (
    "查询腾讯频道（QQ 频道）的只读信息。用 command 指定要查什么、params 传该命令的参数"
    "（JSON 对象，键名见下面清单；标 * 的是必填）。\n"
    "常用场景：想看这个 bot 加入了哪些频道 → get-my-join-guild-info（含频道号/频道 ID）；"
    "想看频道里有哪些版块（板块）→ get-guild-channel-list；"
    "想看频道最近帖子 → latest-feeds-detail / get-guild-feeds；"
    "想知道别人评论了什么 → get-feed-comments / get-notices；"
    "想知道某个人的 tiny_id（@他时必须）→ guild-member-search；"
    "想看频道资料 → get-guild-info。\n"
    "多频道：插件可能同时监听多个频道，涉及 guild_id 的命令可以显式传别的频道 ID 或频道号；"
    "不传则用配置里的默认频道。\n"
    "当前可用命令：\n{capabilities}"
)

DEFAULT_WRITE_ACTION_PROMPT = (
    "对腾讯频道执行写操作（发内容 / 点赞 / 管理）。用 command 指定动作、params 传参数"
    "（JSON 对象，键名见下面清单；标 * 的是必填）。\n"
    "发帖/评论要指定版块时：channel_id 可以填**版块名**（如「闲聊」「全部」）或版块 ID，"
    "不确定版块名就用 channel_sections 查；不传则用配置里的默认版块。\n"
    "⚠️ 破坏性动作（删除/踢人/禁言/权限变更/退出频道）必须把 confirm 设为 true 才会执行；"
    "已被配置关闭的命令会直接拒绝，不要反复重试。\n"
    "当前可用命令：\n{capabilities}"
)

#: 各组件的内置默认提示词（config.toml ``[prompts]`` 的种子值；键 = 配置字段名）
DEFAULT_COMPONENT_PROMPTS: dict[str, str] = {
    "notices": (
        "查看腾讯频道最近的互动通知（收到评论、收到回复、被@、点赞、顶帖）。"
        "返回通知类型、发送者、内容、帖子 ID 与评论 ID，可用于后续评论或回复。"
    ),
    "search_feeds": "在当前腾讯频道内按关键词搜索帖子，返回帖子 ID、标题与摘要。需要账号已加入该频道。",
    "feed_detail": "查看指定帖子的详情（作者、时间、正文、分享链接），返回内容可用于评论或回复。",
    "guild_info": "查看腾讯频道基本信息（名称、成员数、公告、加入设置等）。",
    "members": (
        "按昵称搜索腾讯频道成员，返回成员的 tiny_id（内部用户 ID）。"
        "@某人时必须先用本工具拿到 tiny_id，严禁使用 QQ 号或猜测值。"
    ),
    "guilds": (
        "查看本账号加入/创建/管理的腾讯频道列表，返回频道名称、频道号（pd…）与真实频道 ID。"
        "多频道场景下用它确认该操作哪个频道；返回的频道号或频道 ID 都能当其它工具的 guild_id 用。"
    ),
    "sections": (
        "查看腾讯频道内的版块（板块）列表，返回版块名称与版块 ID。"
        "发帖要指定发到哪个版块时：把这里的版块名或版块 ID 传给 channel_publish_feed 的 channel_id 即可。"
    ),
    "publish_feed": (
        "在腾讯频道发帖。**要指定发到哪个版块**：把版块名（如「闲聊」「全部」）或版块 ID 传给 channel_id，"
        "不确定时先用 channel_sections 查；不传则用插件配置里的默认版块。"
        "不填 title 是短贴（纯文本 ≤1000 字）；填了 title 就是长贴（≤10000 字，"
        "可用 markdown_content 发 Markdown 富文本）。可带图片（短贴≤18、长贴≤50）与视频（短贴 1、长贴 5）。"
        "正文内联语法：@[昵称](tinyid)、[文字](https://url)、#[话题名]()；裸 URL 不可点击。"
        "content_file 只能读实例 data/ 目录下的 txt/md。"
    ),
    "comment_feed": "评论腾讯频道里的一条帖子（帖子级评论）。需要帖子 ID；缺少帖子的创建时间时会自动查询补全。",
    "reply_comment": (
        "楼中楼回复腾讯频道里的某条评论。插件会自动补齐 do-reply 的必填字段；"
        "字段实在不全时会降级为该帖子的帖内评论，并在结果中说明。"
    ),
    "like": (
        "给腾讯频道里的帖子、评论或楼中楼回复点赞（cancel=true 则取消自己点的赞）。"
        "只给 feed_id → 赞帖子；再给 comment_id(c_…) → 赞那条评论；再给 reply_id(r_…) → 赞那条楼中楼回复。"
    ),
    "send_dm": (
        "给腾讯频道用户发送私信。需要对方的 tiny_id（先用 channel_members 按昵称查询）。"
        "注意：对方回复之前只能发 1 条（超发会返回 retCode 100707）。"
    ),
}

#: 组件名 → ``[prompts]`` 字段名
PROMPT_KEYS: dict[str, str] = {
    "channel_notices": "notices",
    "channel_search_feeds": "search_feeds",
    "channel_feed_detail": "feed_detail",
    "channel_guild_info": "guild_info",
    "channel_members": "members",
    "channel_guilds": "guilds",
    "channel_sections": "sections",
    "channel_read": "read_tool",
    "channel_publish_feed": "publish_feed",
    "channel_comment_feed": "comment_feed",
    "channel_reply_comment": "reply_comment",
    "channel_like": "like",
    "channel_send_dm": "send_dm",
    "channel_write": "write_action",
}

#: 专用只读工具 → 它代表的能力（开关关了就把该工具从提示词里摘掉）
READ_TOOL_CAPABILITY: dict[str, str] = {
    "channel_notices": "feed.get-notices",
    "channel_search_feeds": "feed.search-guild-feeds",
    "channel_feed_detail": "feed.get-feed-detail",
    "channel_guild_info": "manage.get-guild-info",
    "channel_members": "manage.guild-member-search",
    "channel_guilds": "manage.get-my-join-guild-info",
    "channel_sections": "manage.get-guild-channel-list",
}

#: 专用写入动作 → 它代表的能力（go_activate 用；任一开启即保留该动作）
ACTION_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "channel_publish_feed": ("feed.publish-feed",),
    "channel_comment_feed": ("feed.do-comment",),
    "channel_reply_comment": ("feed.do-reply",),
    "channel_like": ("feed.do-like", "feed.do-feed-prefer"),
    "channel_send_dm": ("manage.push-group-dm-msg",),
}


def tool_visible(config: Any, component: str) -> bool:
    """某个**工具**是否应该出现在提示词里。"""
    if not plugin_enabled(config):
        return False  # 插件整体停用：本插件的工具全部隐藏
    if component == "channel_read":
        return exposure(config)[0]
    capability = READ_TOOL_CAPABILITY.get(component)
    if capability is None:
        return True  # 不是本插件的能力型工具
    return capability_enabled(config, capability)


# ── 配置读取（兼容配置缺字段 / 缺对象）────────────────────────


def _cfg(config: Any, section: str, field: str, default: Any) -> Any:
    """从配置对象里安全取值。"""
    sec = getattr(config, section, None) if config is not None else None
    if sec is None:
        return default
    value = getattr(sec, field, default)
    return default if value is None else value


def _name_list(config: Any, field: str) -> list[str]:
    raw = _cfg(config, "capabilities", field, [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def plugin_enabled(config: Any) -> bool:
    """插件总开关（``[plugin] enabled``）。

    关掉后：适配器不轮询、不做登录流程，工具与动作也不进 LLM 的可用组件。
    """
    return bool(_cfg(config, "plugin", "enabled", True))


def kind_default_enabled(config: Any, kind: str) -> bool:
    """该类能力的兜底默认开关。"""
    fallback = kind in (KIND_READ, KIND_WRITE)
    return bool(_cfg(config, "capabilities", f"{kind}_default", fallback))


def capability_enabled(config: Any, name: str) -> bool:
    """某个能力是否开启。

    ``disabled`` 优先级最高，其次 ``enabled``，最后按类别兜底。
    三个名单里都可以写：具体能力名（``feed.del-feed``）、类别名（``danger``）、``*``。
    """
    cap = BY_NAME.get(str(name))
    if cap is None:
        return False
    enabled = _name_list(config, "enabled")
    disabled = _name_list(config, "disabled")
    tokens = {cap.name, cap.domain, cap.kind, "*"}
    if tokens & set(disabled):
        return False
    if tokens & set(enabled):
        return True
    return kind_default_enabled(config, cap.kind)


def enabled_capabilities(config: Any, kinds: Iterable[str] = (KIND_READ,)) -> list[Capability]:
    """按类别取出当前开启的能力（保持清单顺序）。"""
    wanted = {str(k) for k in kinds}
    return [cap for cap in CAPABILITIES if cap.kind in wanted and capability_enabled(config, cap.name)]


def any_enabled(config: Any, kinds: Iterable[str]) -> bool:
    """给定类别里是否有任一能力开启。"""
    return bool(enabled_capabilities(config, kinds))


# ── 提示词装配 ──────────────────────────────────────────────


def _hint_overrides(config: Any) -> dict[str, str]:
    """解析 ``[prompts] capability_hints``（元素形如 ``"feed.del-feed=说明"``）。"""
    raw = _cfg(config, "prompts", "capability_hints", [])
    result: dict[str, str] = {}
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return result
    for item in raw:
        text = str(item)
        if "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def capability_hint(config: Any, cap: Capability) -> str:
    """单个能力的提示词（配置覆盖 > 官方说明）。

    有覆盖时把官方说明追加在括号里——覆盖文案可能是简写或口语化，
    保留原始说明能让模型知道这条命令到底做什么。
    """
    override = _hint_overrides(config).get(cap.name)
    if override and cap.summary and override != cap.summary:
        return f"{override}（原说明：{cap.summary}）"
    return override or cap.summary or cap.name


def capability_line(config: Any, cap: Capability) -> str:
    """清单里的一行：``- <能力名>（参数：a*, b）— 说明``。"""
    params = ", ".join(f"{p}*" if p in cap.required else p for p in cap.params)
    suffix = f"（参数：{params}）" if params else "（无参数）"
    return f"- {cap.name}{suffix} — {capability_hint(config, cap)}"


def capabilities_block(config: Any, kinds: Iterable[str]) -> str:
    """把开启的能力渲染成提示词里的清单（按类别分组）。"""
    caps = enabled_capabilities(config, kinds)
    if not caps:
        return "（当前没有开启任何命令）"
    groups: dict[str, list[Capability]] = {}
    for cap in caps:
        groups.setdefault(cap.kind, []).append(cap)
    label = {
        KIND_READ: "只读",
        KIND_WRITE: "写入",
        KIND_SENSITIVE: "敏感（需在配置里开启）",
        KIND_DANGER: "破坏性（需在配置里开启，且执行时要 confirm=true）",
    }
    lines: list[str] = []
    for kind in KINDS:
        items = groups.get(kind)
        if not items:
            continue
        lines.append(f"[{label.get(kind, kind)}]")
        lines.extend(capability_line(config, cap) for cap in items)
    return "\n".join(lines)


def prompt_text(config: Any, key: str, default: str) -> str:
    """取 ``[prompts]`` 里的提示词；留空/缺失则用内置默认。"""
    value = str(_cfg(config, "prompts", key, "") or "").strip()
    return value or default


def read_tool_description(config: Any) -> str:
    """通用只读工具的描述（``{capabilities}`` 占位符替换成当前开启的清单）。"""
    template = prompt_text(config, "read_tool", DEFAULT_READ_TOOL_PROMPT)
    return template.replace("{capabilities}", capabilities_block(config, (KIND_READ,)))


def write_action_description(config: Any) -> str:
    """通用写入动作的描述（同上）。"""
    template = prompt_text(config, "write_action", DEFAULT_WRITE_ACTION_PROMPT)
    return template.replace(
        "{capabilities}",
        capabilities_block(config, (KIND_WRITE, KIND_SENSITIVE, KIND_DANGER)),
    )


def component_prompt(config: Any, component: str) -> str:
    """取某个组件的最终提示词（通用组件动态生成，其余读配置 + 内置默认）。"""
    if component == "channel_read":
        return read_tool_description(config)
    if component == "channel_write":
        return write_action_description(config)
    key = PROMPT_KEYS.get(component, "")
    if not key:
        return ""
    return prompt_text(config, key, DEFAULT_COMPONENT_PROMPTS.get(key, ""))


def apply_prompt_overrides(config: Any) -> list[str]:
    """把所有组件的提示词按配置刷新一遍（避免在组件里硬编码）。

    框架在每次构建提示词时都会读 ``cls.description``（``LLMTool.to_schema``），
    所以在插件加载时改类属性即可生效。返回已刷新的组件名列表。
    """
    from .actions import ACTIONS
    from .tools import TOOLS

    applied: list[str] = []
    for cls in (*TOOLS, *ACTIONS):
        component = str(getattr(cls, "tool_name", "") or getattr(cls, "action_name", "") or "")
        if not component:
            continue
        text = component_prompt(config, component)
        if not text:
            continue
        # 三个属性都设：框架新版读 ``description``，旧版读 ``tool_description`` /
        # ``action_description``。无条件赋值（不判断是否存在），避免依赖基类实现细节。
        for attr in ("description", "tool_description", "action_description"):
            setattr(cls, attr, text)
        applied.append(component)
    return applied


def exposure(config: Any) -> tuple[bool, bool]:
    """返回 ``(是否注册通用只读工具, 是否注册通用写入动作)``。"""
    return (
        bool(_cfg(config, "capabilities", "expose_generic_read", True))
        and any_enabled(config, (KIND_READ,)),
        bool(_cfg(config, "capabilities", "expose_generic_write", True))
        and any_enabled(config, (KIND_WRITE, KIND_SENSITIVE, KIND_DANGER)),
    )


def describe_excluded() -> str:
    """被排除命令的说明（写进文档/日志）。"""
    return "\n".join(f"- {name}：{reason}" for name, reason in sorted(EXCLUDED.items()))


__all__ = [
    "ACTION_CAPABILITIES",
    "ALL_SPEC_NAMES",
    "BY_NAME",
    "CAPABILITIES",
    "CONFIRM_REQUIRED_KINDS",
    "DEFAULT_COMPONENT_PROMPTS",
    "DEFAULT_READ_TOOL_PROMPT",
    "DEFAULT_WRITE_ACTION_PROMPT",
    "EXCLUDED",
    "KINDS",
    "KIND_DANGER",
    "KIND_READ",
    "KIND_SENSITIVE",
    "KIND_WRITE",
    "PROMPT_KEYS",
    "READ_TOOL_CAPABILITY",
    "SAFE_GUARDS",
    "SPEC_CLI_VERSION",
    "SPEC_GENERATED_AT",
    "Capability",
    "any_enabled",
    "apply_prompt_overrides",
    "capabilities_block",
    "capability_by_name",
    "capability_enabled",
    "capability_hint",
    "capability_line",
    "component_prompt",
    "describe_excluded",
    "enabled_capabilities",
    "exposure",
    "kind_default_enabled",
    "plugin_enabled",
    "prompt_text",
    "read_capabilities",
    "read_tool_description",
    "tool_visible",
    "write_action_description",
    "write_capabilities",
]
