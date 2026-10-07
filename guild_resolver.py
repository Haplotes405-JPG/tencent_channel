"""频道号（形如 ``pd20589127``）→ 真实 ``guild_id`` 的解析层。

腾讯频道里用户看到的是「频道号」，而接口要的是纯数字 ``guild_id``，两者不同。
本模块把「配置里写的值」解析成 ``guild_id``：

1. 配置值本身是纯数字 → 直接当 ``guild_id`` 用（不调 CLI）；
2. 其它情况（``pd20589127`` / ``20589127`` / 自定义频道号）→ 调
   ``manage get-my-join-guild-info`` 列出「我创建的 / 我管理的 / 我加入的」频道，
   按频道号匹配出真实 ``guild_id``；
3. 列表里没有可用的频道号字段时，退回 ``manage search-guild-content --scope channel`` 再找一次；
4. 解析成功后进程内缓存（键=归一化频道号），避免每次调用都拉一次列表。

字段名随 CLI 版本可能是 ``guild_number`` 或 ``guildNumber``
（CLI 二进制里两种都存在，protobuf 侧还有 ``GuildNumber``），因此两种命名都做匹配；
``pd`` 前缀与大小写在比较时被归一化，``pd20589127`` == ``PD20589127`` == ``20589127``。

本模块**不依赖框架**（只用标准库 + 调用方传入的 CLI 客户端），便于直接单测。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger("tencent_channel.guild_resolver")

#: 真实 guild_id 的字段名（CLI 用 snake_case，部分版本/结构可能是 camelCase）
_ID_KEYS: tuple[str, ...] = ("guild_id", "guildId", "guildid", "id")
#: 频道号的字段名
_NUMBER_KEYS: tuple[str, ...] = (
    "guild_number",
    "guildNumber",
    "guild_no",
    "guildNo",
    "guild_num",
    "guildNum",
    "channel_number",
    "channelNumber",
    "number",
)
#: 频道名的字段名
_NAME_KEYS: tuple[str, ...] = ("guild_name", "guildName", "guild_nick", "name", "nickname")

_MAX_DEPTH = 8
_MAX_NODES = 800


def _log(logger: Any, level: str, message: str) -> None:
    """按单参数签名打日志。

    框架的 ``src.kernel.logger.Logger`` 只接受 ``(message, **kwargs)``，
    不支持 printf 风格；标准库 logging 则两种都行 —— 这里统一先把消息拼好再传一个参数。
    """
    target = logger if logger is not None else _logger
    try:
        getattr(target, level)(message)
    except Exception:  # noqa: BLE001 - 日志永不阻断业务
        pass


# ── 归一化 ──────────────────────────────────────────────────


def normalize_number(value: Any) -> str:
    """归一频道号：去空白、去前导 ``pd``、转小写。"""
    text = str(value or "").strip().replace(" ", "").replace("\u3000", "")
    if text[:2].lower() == "pd":
        text = text[2:]
    return text.lower()


def looks_like_guild_id(value: Any) -> bool:
    """纯数字视为真实 ``guild_id``；频道号形如 ``pd20589127``（含字母）。"""
    text = str(value or "").strip()
    return bool(text) and text.isdigit()


def split_guild_refs(value: Any) -> list[str]:
    """把「监听频道」字段的任意写法拆成引用列表（去重保序，纯函数，可离线单测）。

    支持三种写法：

    - 列表 / 元组 / 集合：``["pd11111111", "1234567890"]``（元素本身也可以写成逗号分隔字符串）
    - 逗号（中英文）或顿号分隔的字符串：``"pd11111111, pd22222222"``
    - 单个字符串：``"pd11111111"``

    每一项既可以是真实 guild_id（纯数字），也可以是频道号（``pd…``）。
    """
    if value is None:
        raw_items: list[Any] = []
    elif isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw_items = list(value)
    else:
        raw_items = [value]

    refs: list[str] = []
    for entry in raw_items:
        # 中英文逗号与顿号都当分隔符；列表元素本身写成 "pd111,pd222" 时一并拆开
        normalized = str(entry or "").replace("，", ",").replace("、", ",")
        for part in normalized.split(","):
            text = part.strip()
            if text and text not in refs:
                refs.append(text)
    return refs


def configured_guild_refs(config: Any) -> list[str]:
    """取 ``channel.guilds`` 里的「待解析频道引用」列表（纯函数，可离线单测）。

    三种写法见 :func:`split_guild_refs`；解析规则：纯数字视为真实 ``guild_id``
    （不查询），其余按频道号换算。结果去重且保序；未配置时返回空列表。
    """
    if config is None:
        return []
    section = getattr(config, "channel", None)
    if section is None:
        return []
    return split_guild_refs(getattr(section, "guilds", []))


# ── 版块（频道内的板块）解析 ─────────────────────────────────
#
# 版块 ID 在 QQ 客户端里看不到，所以允许到处写「版块名」：插件按名字去
# ``manage get-guild-channel-list`` 里换算出 ID（见 resolve_section）。


def looks_like_channel_id(value: Any) -> bool:
    """纯数字视为真实版块 ID；其余（如「全部」「闲聊」）当版块名。"""
    text = str(value or "").strip()
    return bool(text) and text.isdigit()


def iter_channel_entries(payload: Any) -> list[dict[str, str]]:
    """从 ``manage get-guild-channel-list`` 的返回里取出 ``[{channel_id, channel_name}]``。

    兼容 CLI 形态（``{"channels": [...]}``）与网关原始形态（``guildInfoList[].channelList[]``，名字是 base64）。
    纯函数、无框架依赖，便于离线单测。
    """
    entries: list[dict[str, str]] = []
    containers = ("channels", "channel_list", "channelList", "guildInfoList", "items", "list", "data", "result")

    def _text(node: Mapping[str, Any], keys: Iterable[str]) -> str:
        return _first_text(node, keys)

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 4 or not isinstance(node, Mapping):
            return
        for container in containers:
            value = node.get(container)
            if isinstance(value, list):
                for item in value:
                    if not isinstance(item, Mapping):
                        continue
                    # 网关的 guildInfoList 条目里还嵌着 channelList
                    nested = item.get("channelList") or item.get("channel_list")
                    if isinstance(nested, list):
                        _walk({"channels": nested}, depth + 1)
                        continue
                    channel_id = _text(item, ("channel_id", "channelId", "id"))
                    if not channel_id:
                        continue
                    name = _text(
                        item,
                        (
                            "channel_name",
                            "channelName",
                            # 网关原始返回里版块名是 bytesChannelName（base64 编码的 UTF-8）
                            "bytesChannelName",
                            "bytes_channel_name",
                            "name",
                        ),
                    )
                    if name:
                        try:  # 网关的 bytesChannelName 是 base64
                            decoded = base64.b64decode(name, validate=True).decode("utf-8")
                            if decoded.isprintable() and len(decoded) > 1:
                                name = decoded
                        except Exception:  # noqa: BLE001 - 不是 base64 就原样用
                            pass
                    entries.append({"channel_id": channel_id, "channel_name": name})
            elif isinstance(value, Mapping):
                _walk(value, depth + 1)

    if isinstance(payload, Mapping):
        _walk(payload)
    return entries


@dataclass
class SectionResolution:
    """版块解析结果。"""

    ok: bool = False
    channel_id: str = ""
    channel_name: str = ""
    message: str = ""
    candidates: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """排障/展示用标签。"""
        return f"{self.channel_name}（{self.channel_id}）" if self.channel_name else self.channel_id


def _section_candidates_text(entries: Iterable[Mapping[str, str]]) -> str:
    return "；".join(
        f"{item.get('channel_name') or '（无名）'}（{item.get('channel_id')}）" for item in entries
    )[:400]


async def resolve_section(
    cli: Any,
    *,
    guild_id: str,
    ref: str = "",
    logger: Any = None,
) -> SectionResolution:
    """把「版块 ID 或版块名」解析成可用的真实版块 ID。

    - 纯数字 → 原样当 ID（不查接口）；
    - 其它（``全部`` / ``闲聊`` …）→ 调 ``manage get-guild-channel-list`` 按名字匹配，
      先精确匹配、再忽略大小写、最后允许包含匹配；
    - 都匹配不上时返回候选列表，调用方把 message 直接回给 LLM / 日志。
    """
    text = str(ref or "").strip()
    if not text:
        return SectionResolution(ok=False, message="没有给出频道 ID 或频道号（guild_id 为空）")
    if looks_like_channel_id(text):
        return SectionResolution(ok=True, channel_id=text, message=f"已是版块 ID（{text}）")
    if not guild_id:
        return SectionResolution(ok=False, message=f"要按版块名「{text}」查 ID，必须先有频道 ID（guild_id）")

    result = await cli.get_channel_list(guild_id)
    payload = getattr(result, "data", None)
    if payload is None:
        payload = getattr(result, "raw", None)
    if not getattr(result, "ok", False):
        _log(logger, "warning", f"按版块名「{text}」解析失败：拉取版块列表不成功（{getattr(result, 'error', '')}）")
        return SectionResolution(ok=False, message=f"拉取版块列表失败：{getattr(result, 'error', '')}")

    entries = iter_channel_entries(payload)
    if not entries:
        return SectionResolution(ok=False, message="接口没有返回任何版块")

    wanted = text.casefold()
    hit = next((e for e in entries if (e.get("channel_name") or "").strip() == text), None)
    if hit is None:
        hit = next((e for e in entries if (e.get("channel_name") or "").strip().casefold() == wanted), None)
    if hit is None:
        hit = next((e for e in entries if wanted in (e.get("channel_name") or "").casefold()), None)
    if hit is None:
        candidates = [e.get("channel_name") or e.get("channel_id") or "" for e in entries]
        return SectionResolution(
            ok=False,
            message=f"频道里没有叫「{text}」的版块。可选：{_section_candidates_text(entries)}",
            candidates=candidates,
        )
    return SectionResolution(
        ok=True,
        channel_id=str(hit.get("channel_id") or ""),
        channel_name=str(hit.get("channel_name") or ""),
        message=f"版块「{hit.get('channel_name')}」→ channel_id={hit.get('channel_id')}",
    )


def _first_text(mapping: Mapping[str, Any], keys: Iterable[str]) -> str:
    """取第一个「标量且非空」的候选键值（dict/list 不算）。"""
    for key in keys:
        if key not in mapping:
            continue
        value = mapping[key]
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                return text
    return ""


# ── 返回结构 ────────────────────────────────────────────────


@dataclass
class GuildEntry:
    """从 CLI 返回里抠出来的一条频道记录。"""

    guild_id: str = ""
    guild_number: str = ""
    guild_name: str = ""
    path: str = ""

    @property
    def label(self) -> str:
        """排障/报错用的可读标签。"""
        parts = [part for part in (self.guild_name, self.guild_number, self.guild_id) if part]
        return " / ".join(parts)


@dataclass
class GuildResolution:
    """一次频道解析的结果。"""

    ok: bool = False
    guild_id: str = ""
    guild_number: str = ""
    guild_name: str = ""
    message: str = ""
    from_cache: bool = False
    candidates: list[str] = field(default_factory=list)

    def hint(self) -> str:
        """给 LLM/运维看的一句话。"""
        return self.message or ("已解析" if self.ok else "频道解析失败")


# ── 解析返回里的频道记录 ────────────────────────────────────


def iter_guild_entries(payload: Any) -> list[GuildEntry]:
    """有界深度优先遍历返回结构，收集所有形如「频道」的 dict。

    父节点的 ``guild_id`` 会作为 ``inherited_id`` 传给孩子节点，
    这样「频道号在子结构里、ID 在父结构里」的排布也能匹配上。
    """
    entries: list[GuildEntry] = []
    visited = 0

    def walk(node: Any, inherited_id: str, path: str, depth: int) -> None:
        nonlocal visited
        if visited >= _MAX_NODES or depth > _MAX_DEPTH:
            return
        if isinstance(node, Mapping):
            visited += 1
            own_id = _first_text(node, _ID_KEYS)
            guild_id = own_id or inherited_id
            number = _first_text(node, _NUMBER_KEYS)
            name = _first_text(node, _NAME_KEYS)
            if guild_id:
                entries.append(
                    GuildEntry(guild_id=guild_id, guild_number=number, guild_name=name, path=path)
                )
            for key, value in node.items():
                if isinstance(value, (Mapping, list, tuple)):
                    walk(value, guild_id, f"{path}.{key}" if path else str(key), depth + 1)
        elif isinstance(node, (list, tuple)):
            for index, item in enumerate(list(node)[: _MAX_NODES]):
                walk(item, inherited_id, f"{path}[{index}]", depth + 1)

    walk(payload, "", "", 0)
    return entries


def match_entry(entries: Iterable[GuildEntry], number: Any) -> GuildEntry | None:
    """按归一化频道号匹配频道记录。"""
    target = normalize_number(number)
    if not target:
        return None
    for entry in entries:
        if entry.guild_number and normalize_number(entry.guild_number) == target:
            return entry
    return None


def _candidates_text(entries: Iterable[GuildEntry]) -> str:
    """把候选频道渲染成一行提示（有频道号优先）。"""
    items = list(entries)
    with_number = [e for e in items if e.guild_number][:10]
    if with_number:
        return "；".join(e.label for e in with_number)
    named = [e for e in items if e.guild_name][:10]
    if named:
        return "；".join(f"{e.guild_name}（ID={e.guild_id}）" for e in named)
    return "，".join(e.guild_id for e in items if e.guild_id)[:400]


def _payload_of(result: Any) -> Any:
    """取 CliResult 里的业务数据（data 优先，其次 raw）。"""
    data = getattr(result, "data", None)
    return data if data is not None else getattr(result, "raw", None)


# ── 缓存 ────────────────────────────────────────────────────

_CACHE: dict[str, GuildResolution] = {}
_LOCK = asyncio.Lock()


def peek_cached(number: Any) -> GuildResolution | None:
    """读缓存（不触发解析）。"""
    key = normalize_number(number)
    return _CACHE.get(key) if key else None


def forget_cache(number: Any = "") -> None:
    """清缓存（传空清全部）—— 主要给测试与配置热重载用。"""
    if not number:
        _CACHE.clear()
        return
    _CACHE.pop(normalize_number(number), None)


# ── 解析 ────────────────────────────────────────────────────


def _failure(*, number: str, message: str, entries: Iterable[GuildEntry] = ()) -> GuildResolution:
    candidates = [
        e.label if e.guild_number or e.guild_name else e.guild_id
        for e in entries
        if e.guild_id
    ][:10]
    return GuildResolution(
        ok=False,
        guild_number=number,
        message=message,
        candidates=candidates,
    )


async def _lookup(cli: Any, *, number: str, logger: Any = None) -> GuildResolution:
    """真正的解析动作：先查「我的频道」列表，再退回频道搜索。"""
    result = await cli.get_my_guilds()
    if not result.ok:
        hint = result.hint() if hasattr(result, "hint") else ""
        _log(logger, "warning", f"解析频道号失败：拉取「我的腾讯频道」不成功（{result.error}）")
        return _failure(
            number=number,
            message=(
                f"解析频道号 {number} 失败：拉取「我的腾讯频道」不成功（{result.error}）。{hint}"
            ),
        )

    entries = iter_guild_entries(_payload_of(result))
    hit = match_entry(entries, number)
    if hit is not None:
        return GuildResolution(
            ok=True,
            guild_id=hit.guild_id,
            guild_number=hit.guild_number or number,
            guild_name=hit.guild_name,
            message=f"频道号 {number} → guild_id {hit.guild_id}"
            + (f"（{hit.guild_name}）" if hit.guild_name else ""),
        )

    # 兜底：按频道号搜索频道（列表里可能没有频道号字段）
    search = getattr(cli, "search_guild_content", None)
    if callable(search):
        _log(logger, "debug", f"「我的腾讯频道」里没匹配到 {number}，改为按频道号搜索")
        try:
            searched = await search(number, scope="channel")
        except Exception as exc:  # noqa: BLE001 - 兜底失败不影响主结论
            _log(logger, "debug", f"按频道号搜索失败：{exc}")
            searched = None
        if searched is not None and getattr(searched, "ok", False):
            hit = match_entry(iter_guild_entries(_payload_of(searched)), number)
            if hit is not None:
                return GuildResolution(
                    ok=True,
                    guild_id=hit.guild_id,
                    guild_number=hit.guild_number or number,
                    guild_name=hit.guild_name,
                    message=f"频道号 {number} → guild_id {hit.guild_id}（来自频道搜索）",
                )

    has_numbers = any(e.guild_number for e in entries)
    ids = [e.guild_id for e in entries if e.guild_id]
    if not ids:
        message = (
            f"解析频道号 {number} 失败：`manage get-my-join-guild-info` 没有返回任何频道。"
            "请确认该账号已加入目标频道（`manage search-and-join --keyword \"频道名\"`）。"
        )
    elif has_numbers:
        message = (
            f"频道号 {number} 在「我的腾讯频道」里没有匹配项。"
            f"可选：{_candidates_text(entries) or '（无）'}。"
            "请把 channel.guilds 改成上面的真实 ID（或修正频道号）。"
        )
    else:
        message = (
            f"频道列表里没有「频道号」字段（CLI 版本可能不同），无法自动把 {number} 换成真实 ID。"
            f"该账号的频道：{_candidates_text(entries) or '（无）'}。"
            "请直接把真实 guild_id 填进 channel.guilds。"
        )
    _log(logger, "warning", message)
    return _failure(number=number, message=message, entries=entries)


async def resolve_guild(
    cli: Any,
    *,
    guild_id: str = "",
    guild_number: str = "",
    refresh: bool = False,
    logger: Any = None,
) -> GuildResolution:
    """把配置值解析成真实 ``guild_id``（带进程内缓存）。

    Args:
        cli: ``TencentChannelCli`` 实例（鸭子类型，便于测试注入假对象）。
        guild_id: ``channel.guilds`` 里的一个引用（纯数字视为真实 ID，否则当频道号）。
        guild_number: 兼容旧调用方的「频道号」入参（可空；``guild_id`` 已够用时不必传）。
        refresh: 忽略缓存强制重新解析。
        logger: 可选日志对象（框架 Logger 或标准库 logger）。

    Returns:
        GuildResolution: ``ok=True`` 时 ``guild_id`` 是可直接调用的真实 ID。
    """
    raw_id = str(guild_id or "").strip()
    raw_number = str(guild_number or "").strip()

    if looks_like_guild_id(raw_id):
        return GuildResolution(
            ok=True,
            guild_id=raw_id,
            guild_number=normalize_number(raw_number),
            message=f"channel.guilds 里已是真实频道 ID（{raw_id}）",
        )

    number = raw_number or raw_id
    if not number:
        return GuildResolution(
            ok=False,
            message=(
                "缺少频道：请在插件配置 channel.guilds 里填监听频道"
                "（可直接填频道号，形如 pd20589127，插件会自动解析；也可填纯数字的真实 ID）。"
            ),
        )

    key = normalize_number(number)
    if not refresh:
        cached = _CACHE.get(key)
        if cached is not None:
            return GuildResolution(**{**cached.__dict__, "from_cache": True})

    async with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and not refresh:
            return GuildResolution(**{**cached.__dict__, "from_cache": True})
        resolution = await _lookup(cli, number=number, logger=logger)
        if resolution.ok:
            _CACHE[key] = resolution
        return resolution
