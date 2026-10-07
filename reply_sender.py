"""回复规划与发送策略（无框架依赖）。

把「通知上下文 → 走哪条 CLI 命令 → 缺必填字段时如何补齐 / 降级」集中在这里，
让 Adapter（自动回复）与 Action（LLM 主动回复）共用同一策略，并可脱离框架单测。

关键约束（来自 CLI 文档与 v1.0.10 schema）：

- ``feed do-reply`` 必填：``feed_id / feed_author_id / feed_create_time /
  comment_id / comment_author_id / comment_create_time / replier_id``（+ ``content``）；
  ``--reply-type`` 固定 1（发表），0/2 是删除，绝不允许。
- ``feed do-comment`` 必填：``feed_id / feed_create_time``（+ ``content``）；
  ``--comment-type`` 固定 1。
- ``manage push-group-dm-msg`` 主动发送需要 ``peer_tiny_id`` + ``source_guild_id``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .cli_client import CliResult
from .notice_mapping import extract_replies, find_field

#: do-reply 的必填字段（除 content / feed_id / comment_id 之外）
REQUIRED_REPLY_FIELDS: tuple[str, ...] = (
    "feed_author_id",
    "feed_create_time",
    "comment_author_id",
    "comment_create_time",
    "replier_id",
)

_FEED_AUTHOR_KEYS = ("feed_author_id", "feedAuthorId", "author_id", "authorId", "poster_id", "posterId")
_FEED_CREATE_KEYS = (
    "feed_create_time",
    "feedCreateTime",
    "create_time_raw",
    "createTimeRaw",
    "create_time",
    "createTime",
)
_COMMENT_AUTHOR_KEYS = ("comment_author_id", "commentAuthorId", "author_id", "authorId", "replier_id")
_COMMENT_CREATE_KEYS = (
    "comment_create_time",
    "commentCreateTime",
    "create_time_raw",
    "createTimeRaw",
    "create_time",
    "createTime",
)
_COMMENT_ID_KEYS = ("comment_id", "commentId")


def _s(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


#: 超过这个量级就当作毫秒时间戳
_MS_THRESHOLD = 100_000_000_000

#: 通知里可能出现的人类可读时间格式
_TIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def to_epoch_seconds(value: Any) -> str:
    """把时间统一成「秒级时间戳字符串」。

    接口的 ``createTime`` 只认秒级时间戳：``feed get-notices`` 给的是
    ``"2026-10-01 10:19:38"`` 这种人类可读时间（而且那是**通知时间**，不等于帖子创建时间），
    而 ``feed get-feed-detail`` 给的是 ``create_time_raw``（秒级）。

    兼容：秒级/毫秒级整数与数字字符串、上面几种人类可读格式。
    认不出来时返回空串，调用方据此决定补查或省略该字段。
    """
    text = _s(value)
    if not text:
        return ""

    if text.isdigit():
        number = int(text)
        return str(number // 1000 if number > _MS_THRESHOLD else number)

    try:
        number = float(text)
    except ValueError:
        number = 0.0
    if number > 0:
        return str(int(number / 1000 if number > _MS_THRESHOLD else number))

    for fmt in _TIME_FORMATS:
        try:
            return str(int(datetime.strptime(text, fmt).timestamp()))
        except ValueError:
            continue
    return ""


def is_epoch_seconds(value: Any) -> bool:
    """该值能否当成秒级时间戳直接发给接口。"""
    return bool(to_epoch_seconds(value))


def truncate(text: str, max_length: int) -> str:
    """按字符数截断（超长追加省略号）。"""
    text = text or ""
    if max_length <= 0 or len(text) <= max_length:
        return text
    return text[: max(0, max_length - 1)] + "…"


@dataclass
class ReplyOutcome:
    """一次回复尝试的结果。"""

    ok: bool
    mode: str
    message: str
    result: CliResult | None = None
    missing: tuple[str, ...] = ()
    enriched: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def _find_comment(payload: Any, comment_id: str, *, depth: int = 4) -> Mapping[str, Any] | None:
    """在评论列表返回里定位指定 comment_id 的评论对象。"""
    if not comment_id or depth <= 0:
        return None
    if isinstance(payload, Mapping):
        if _s(find_field(payload, _COMMENT_ID_KEYS, depth=0)) == comment_id:
            return payload
        for value in payload.values():
            found = _find_comment(value, comment_id, depth=depth - 1)
            if found is not None:
                return found
    elif isinstance(payload, (list, tuple)):
        for item in list(payload)[:50]:
            found = _find_comment(item, comment_id, depth=depth - 1)
            if found is not None:
                return found
    return None


def find_comment_payload(payload: Any, comment_id: str, *, depth: int = 4) -> Mapping[str, Any] | None:
    """在评论列表返回里定位指定 ``comment_id`` 的对象（公开入口，供 adapter 复用）。"""
    return _find_comment(payload, comment_id, depth=depth)


def find_reply_context(payload: Any, reply_id: str, *, max_depth: int = 6) -> dict[str, Any] | None:
    """定位 ``reply_id`` 所属的一级评论，并取出 do-reply 需要的信息（纯函数，可离线单测）。

    CLI 契约（``feed do-reply`` 场景 2「回复某条回复」）：
    ``comment_id`` 必须是**一级评论** ID，被回复的那条回复放在
    ``target_reply_id`` / ``target_user_id`` / ``target_user_nick``。
    """
    if not reply_id:
        return None

    def walk(node: Any, depth: int) -> dict[str, Any] | None:
        if depth <= 0:
            return None
        if isinstance(node, Mapping):
            cid = _s(node.get("comment_id"))
            if cid:
                for reply in extract_replies(node):
                    if _s(reply.get("reply_id")) != reply_id:
                        continue
                    target_user_id = _s(find_field(reply, _COMMENT_AUTHOR_KEYS, depth=0))
                    return {
                        "comment_id": cid,
                        "comment_author_id": _s(find_field(node, _COMMENT_AUTHOR_KEYS, depth=0)),
                        "comment_create_time": to_epoch_seconds(
                            find_field(node, _COMMENT_CREATE_KEYS, depth=0)
                        ),
                        "target_reply_id": reply_id,
                        "target_user_id": target_user_id,
                        "target_user_nick": _s(
                            find_field(reply, ("author", "author_nick", "nickname", "nick_name"), depth=0)
                        )
                        or target_user_id,
                    }
            for value in node.values():
                found = walk(value, depth - 1)
                if found is not None:
                    return found
        elif isinstance(node, (list, tuple)):
            for item in list(node)[:50]:
                found = walk(item, depth - 1)
                if found is not None:
                    return found
        return None

    return walk(payload, max_depth)


async def resolve_reply_target(
    client: Any,
    feed_id: str,
    reply_id: str,
    *,
    guild_id: str = "",
    channel_id: str = "",
    count: int = 20,
    reply_list_num: int = 10,
) -> dict[str, Any]:
    """把「回复 ID」换算成 do-reply 需要的形状；找不到时返回空 dict。

    注意 ``reply_list_num`` 要给足（默认取上限 10），否则回复不在预览里就找不到。
    """
    result = await client.get_feed_comments(
        feed_id,
        guild_id=guild_id,
        channel_id=channel_id,
        count=count,
        reply_list_num=reply_list_num,
    )
    if not result.ok:
        return {}
    return find_reply_context(result.data, reply_id) or {}


async def enrich_feed_meta(
    client: Any, feed_id: str, *, guild_id: str = "", channel_id: str = ""
) -> dict[str, Any]:
    """用 ``feed get-feed-detail`` 补齐帖子级字段（时间统一成秒级时间戳）。"""
    result = await client.get_feed_detail(feed_id, guild_id=guild_id, channel_id=channel_id)
    if not result.ok or not isinstance(result.data, Mapping):
        return {}
    return {
        "feed_author_id": _s(find_field(result.data, _FEED_AUTHOR_KEYS)),
        "feed_create_time": to_epoch_seconds(find_field(result.data, _FEED_CREATE_KEYS)),
    }


async def enrich_comment_meta(
    client: Any,
    feed_id: str,
    comment_id: str,
    *,
    guild_id: str = "",
    channel_id: str = "",
    count: int = 20,
) -> dict[str, Any]:
    """用 ``feed get-feed-comments`` 补齐评论级字段（时间统一成秒级时间戳）。"""
    result = await client.get_feed_comments(
        feed_id, guild_id=guild_id, channel_id=channel_id, count=count
    )
    if not result.ok:
        return {}
    match = _find_comment(result.data, comment_id)
    source: Any = match if match is not None else result.data
    return {
        "comment_author_id": _s(find_field(source, _COMMENT_AUTHOR_KEYS)),
        "comment_create_time": to_epoch_seconds(find_field(source, _COMMENT_CREATE_KEYS)),
    }


async def send_comment_reply(
    client: Any,
    *,
    ctx: Mapping[str, Any],
    text: str,
    guild_id: str = "",
    channel_id: str = "",
    self_tiny_id: str = "",
    reply_to_comment: bool = True,
    enrich: bool = True,
    image_path: str = "",
) -> ReplyOutcome:
    """把回复发回帖子评论区：优先楼中楼（do-reply），字段不全时降级为帖内评论（do-comment）。"""
    merged: dict[str, Any] = dict(ctx)
    feed_id = _s(merged.get("feed_id"))
    feed_subject = _s(merged.get("feed_title")) or feed_id
    if not feed_id:
        return ReplyOutcome(False, "skipped", "通知上下文缺少 feed_id，无法回复（已跳过，避免发错位置）")

    target_guild = guild_id or _s(merged.get("guild_id"))
    target_channel = channel_id or _s(merged.get("channel_id"))
    if self_tiny_id and not _s(merged.get("replier_id")):
        merged["replier_id"] = self_tiny_id

    comment_id = _s(merged.get("comment_id"))
    # LLM 有时把「回复 ID」(r_…) 当成 comment_id 传进来。接口的 comment_id 只接受
    # 一级评论 ID：直接传 r_… 时服务端会静默忽略（CLI 仍报 ret_code=0/success），
    # 现象就是"日志说成功了，频道里什么都没有"。这里自动换算成契约里的场景 2 形状。
    if comment_id.lower().startswith("r_"):
        resolved = await resolve_reply_target(
            client, feed_id, comment_id, guild_id=target_guild, channel_id=target_channel
        )
        if not resolved:
            return ReplyOutcome(
                False,
                "skipped",
                f"无法把回复 ID {comment_id} 换算成它所属的一级评论（评论列表里没找到），"
                "已跳过以免把回复发到错误位置",
            )
        merged.update(resolved)
        comment_id = _s(merged.get("comment_id"))
    enriched = False

    # 帖子级字段：接口要的 createTime 是「秒级时间戳」，而通知里给的是人类可读时间
    # （"2026-10-01 10:19:38"，而且还是通知时间、不等于帖子创建时间），
    # 所以这里一律以 get-feed-detail 的 create_time_raw 为准 —— 否则会 retCode=8010
    # 「字段 createTime 格式不正确」。
    if enrich:
        feed_meta = await enrich_feed_meta(
            client, feed_id, guild_id=target_guild, channel_id=target_channel
        )
        if feed_meta.get("feed_create_time"):
            merged["feed_create_time_raw"] = feed_meta["feed_create_time"]
            enriched = True
        if feed_meta.get("feed_author_id") and not _s(merged.get("feed_author_id")):
            merged["feed_author_id"] = feed_meta["feed_author_id"]
            enriched = True

    feed_create_time = to_epoch_seconds(merged.get("feed_create_time_raw")) or to_epoch_seconds(
        merged.get("feed_create_time")
    )
    if feed_create_time:
        merged["feed_create_time"] = feed_create_time
    else:
        merged["feed_create_time"] = ""

    # 评论级字段：只有楼中楼（do-reply）需要，缺了才去查评论列表
    if enrich and reply_to_comment and comment_id:
        if not _s(merged.get("comment_author_id")) or not is_epoch_seconds(
            merged.get("comment_create_time")
        ):
            comment_meta = await enrich_comment_meta(
                client, feed_id, comment_id, guild_id=target_guild, channel_id=target_channel
            )
            if comment_meta.get("comment_author_id") and not _s(merged.get("comment_author_id")):
                merged["comment_author_id"] = comment_meta["comment_author_id"]
                enriched = True
            if comment_meta.get("comment_create_time") and not is_epoch_seconds(
                merged.get("comment_create_time")
            ):
                merged["comment_create_time"] = comment_meta["comment_create_time"]
                enriched = True

    comment_create_time = to_epoch_seconds(merged.get("comment_create_time"))
    merged["comment_create_time"] = comment_create_time
    missing = tuple(name for name in REQUIRED_REPLY_FIELDS if not _s(merged.get(name)))

    async def _comment(reason: str) -> ReplyOutcome:
        """发成帖内评论（do-comment）。"""
        result = await client.do_comment(
            text,
            feed_id=feed_id,
            feed_create_time=feed_create_time,
            guild_id=target_guild,
            channel_id=target_channel,
            image_path=image_path,
        )
        if result.ok:
            message = f"已作为帖内评论回复（帖子：{feed_subject}）"
            if reason:
                message += f"；{reason}"
        else:
            message = f"帖内评论回复失败：{result.error}"
            if not feed_create_time:
                message += "（提示：没取到帖子的秒级创建时间，确认 reply.enrich_comment_context=true）"
        return ReplyOutcome(result.ok, "do-comment", message, result, missing, enriched)

    # 帖子级通知（没有评论 ID）或配置关闭楼中楼：直接评论帖子，这不算降级
    if not comment_id:
        return await _comment("通知不含评论 ID，按帖子级评论回复")
    if not reply_to_comment:
        return await _comment("配置 reply.reply_to_comment=false，按帖子级评论回复")

    # 有评论 ID 但 do-reply 必填字段不全：降级为帖内评论
    if missing:
        return await _comment(f"do-reply 缺字段 {', '.join(missing)}，已降级")

    extra_fields: dict[str, Any] = {
        "feed_author_id": merged.get("feed_author_id") or "",
        "feed_create_time": feed_create_time,
        "comment_author_id": merged.get("comment_author_id") or "",
        "comment_create_time": merged.get("comment_create_time") or "",
        "replier_id": merged.get("replier_id") or "",
        "target_reply_id": merged.get("target_reply_id") or "",
        "target_user_id": merged.get("target_user_id") or merged.get("comment_author_id") or "",
        "target_user_nick": merged.get("from_nickname") or merged.get("target_user_nick") or "",
    }
    result = await client.do_reply(
        text,
        feed_id=feed_id,
        comment_id=comment_id,
        extra_fields=extra_fields,
        guild_id=target_guild,
        channel_id=target_channel,
        image_path=image_path,
    )
    if result.ok:
        return ReplyOutcome(
            True,
            "do-reply",
            f"已楼中楼回复评论（帖子：{feed_subject}，评论：{comment_id}）",
            result,
            (),
            enriched,
        )
    # do-reply 被接口拒绝时不放弃：退回帖内评论，保证这条回复一定发得出去。
    # （回退原因写进 message，适配器会以「自动回复成功/失败：<message>」打到日志里）
    fallback = await _comment(f"do-reply 失败（{result.error}），已回退为帖内评论")
    if fallback.ok:
        return ReplyOutcome(True, "do-comment", fallback.message, fallback.result, (), enriched)
    return ReplyOutcome(
        False,
        "do-reply",
        f"楼中楼回复失败：{result.error}；帖内评论回退也失败：{fallback.message}",
        result,
        (),
        enriched,
    )


async def send_dm_reply(
    client: Any,
    *,
    ctx: Mapping[str, Any],
    text: str,
    source_guild_id: str = "",
    peer_tiny_id: str = "",
) -> ReplyOutcome:
    """把回复发成私信（``manage push-group-dm-msg``，主动发送模式）。"""
    peer = peer_tiny_id or _s(ctx.get("from_user_id")) or _s(ctx.get("peer_tiny_id"))
    source = source_guild_id or _s(ctx.get("source_guild_id")) or _s(ctx.get("guild_id"))
    if not peer:
        return ReplyOutcome(False, "skipped", "通知上下文缺少对方 tiny_id，无法私信回复")
    if not source:
        return ReplyOutcome(
            False,
            "skipped",
            "缺少 source_guild_id（请配置 channel.dm_source_guild_id 或 channel.guilds）",
        )
    result = await client.push_dm(text, peer_tiny_id=peer, source_guild_id=source)
    message = "已私信回复" if result.ok else f"私信回复失败：{result.error}"
    return ReplyOutcome(result.ok, "dm", message, result)
