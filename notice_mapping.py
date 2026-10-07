"""互动通知 → MoFox ``MessageEnvelope`` 的纯函数映射层。

零框架依赖：不 import ``src.*`` / ``mofox_wire``，因此可独立单测。

已核实的关键约束（Neo-MoFox 源码）
----------------------------------
- ``direction`` 必须是 ``"incoming"``，否则接收器静默丢弃。
- ``message_info`` 必填；``message_segment`` 必填（单段可以是 dict）。
- 群聊 / 私聊**没有独立字段**：有 ``group_info`` 即群聊，否则私聊。
- ``stream_id`` 由框架推导：``sha256(f"{platform}_{group_id}")``（群）或
  ``sha256(f"{platform}_{user_id}_private")``（私聊）。
- ``message_info.message_type`` 若不填则走「有 segment 即标准消息」的兼容分支（本项目采用）。

路由设计（本插件的约定）
------------------------
把路由信息编码进 ``group_id``，这样即使插件重启、内存上下文丢失，
出站时仍能从出站 envelope 的 ``group_info.group_id`` 反解出目标：

- 帖子级：``tcf|{guild_id}|{feed_id}``
- 评论级：``tcf|{guild_id}|{feed_id}|{comment_id}``
- 私信：走私聊 stream，``user_id = {peer_tiny_id}``
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: 路由键前缀
ROUTE_PREFIX = "tcf"

#: 通知源字段候选名（真实字段名未文档化，做宽容探测；P0 探测后可在 docs/cli-contract.md 收敛）
_ID_KEYS = ("notice_id", "noticeId", "id", "msgSeq", "msg_seq", "seq")
_TYPE_KEYS = ("notice_type", "noticeType", "type", "bizType", "biz_type", "notice_type_name")
_GUILD_KEYS = ("guild_id", "guildId", "guild_id_str")
_CHANNEL_KEYS = ("channel_id", "channelId", "channel_id_str")
_FEED_KEYS = ("feed_id", "feedId", "feed_id_str")
_FEED_TITLE_KEYS = (
    "feed_title",
    "feedTitle",
    "title",
    "topic_content",
    "topicContent",
    "feed_summary",
    "feedSummary",
)
_FEED_AUTHOR_KEYS = ("feed_author_id", "feedAuthorId", "author_id", "authorId")
_FEED_CREATE_TIME_KEYS = (
    "feed_create_time",
    "feedCreateTime",
    "create_time",
    "createTime",
    "msg_time",
    "msgTime",
)
_FEED_CREATE_TIME_RAW_KEYS = ("feed_create_time_raw", "feedCreateTimeRaw", "create_time_raw", "createTimeRaw")
_COMMENT_KEYS = ("comment_id", "commentId")
_COMMENT_AUTHOR_KEYS = ("comment_author_id", "commentAuthorId")
_COMMENT_CREATE_TIME_KEYS = ("comment_create_time", "commentCreateTime")
_REPLY_KEYS = ("reply_id", "replyId")
_TARGET_REPLY_KEYS = ("target_reply_id", "targetReplyId")
_TARGET_USER_KEYS = ("target_user_id", "targetUserId")
_FROM_USER_KEYS = (
    "from_tiny_id",
    "fromTinyId",
    "user_id",
    "userId",
    "tiny_id",
    "tinyId",
    "author_id",
    "authorId",
    "poster_id",
    "posterId",
)
_FROM_NICK_KEYS = (
    "from_nick",
    "fromNick",
    "nick_name",
    "nickName",
    "nickname",
    "user_nickname",
    "userNickname",
    "poster_nick",
    "posterNick",
    "author_nick",
    "authorNick",
    "member_name",
    "memberName",
    "name",
)
_CONTENT_KEYS = (
    "content",
    "text",
    "comment_content",
    "commentContent",
    "reply_content",
    "replyContent",
    "summary",
    "digest",
    "title",
)
_CREATE_TIME_RAW_KEYS = ("create_time_raw", "createTimeRaw", "msg_time", "msgTime", "time", "timestamp")
_CREATE_TIME_TEXT_KEYS = ("create_time", "createTime", "create_time_str")
_SOURCE_GUILD_KEYS = ("source_guild_id", "sourceGuildId")

#: 嵌套容器名（字段可能被包一层）
_NESTED_CONTAINERS = (
    "notice",
    "notice_info",
    "noticeInfo",
    "detail",
    "feed",
    "feed_brief",
    "feedBrief",
    "comment",
    "comment_info",
    "commentInfo",
    "reply",
    "poster_info",
    "posterInfo",
    "user_info",
    "userInfo",
    "author",
    "data",
)

#: 数值枚举（feed get-notices）：1 顶帖 / 2 赞评论 / 3 赞回复 / 4 收到评论 / 5 收到回复 / 6 被@
_NUMERIC_CATEGORY: dict[int, str] = {
    1: "top",
    2: "like",
    3: "like",
    4: "comment",
    5: "reply",
    6: "at",
}

#: 字符串枚举（CLI / 服务端返回的 NOTICE_TYPE_* 名称）
_NAME_CATEGORY: tuple[tuple[str, str], ...] = (
    ("DOREPLY", "reply"),
    ("DOCOMMENT", "comment"),
    ("AT_ME", "at"),
    ("DOAT", "at"),
    ("DOLIKE", "like"),
    ("DOPOLYLIKE", "like"),
    ("DOFAVOR", "favorite"),
    ("DOTOP", "top"),
    ("SYSTEM", "system"),
    ("DM", "dm"),
)

#: 值得让 LLM 处理（可回复）的类别
ACTIONABLE_CATEGORIES: frozenset[str] = frozenset({"comment", "reply", "at", "dm", "feed"})

_CATEGORY_LABEL: dict[str, str] = {
    "comment": "收到评论",
    "reply": "收到回复",
    "at": "被@",
    "dm": "私信",
    "feed": "新帖子",
    "like": "点赞",
    "favorite": "收藏",
    "top": "顶帖",
    "system": "系统通知",
    "unknown": "通知",
    # 评论区轮询里「别人之间的对话」用的中性标签：它们不是对机器人说的话，
    # 用「收到评论/收到回复」会让 LLM 以为句句在叫它（实测回复积极性过高）。
    "conversation": "频道评论",
    "conversation_reply": "频道楼中楼",
}

#: 「是否在对机器人说话」的判定用的类别（互动通知路径：通知本来就只来自机器人自己的帖子）
_DIRECTED_CATEGORIES: frozenset[str] = frozenset({"at", "reply", "comment", "dm"})

#: 注入文本的关系说明：写成**自然语言**（「回复了你的留言」），而不是
#: 「【平台标签】+（事实说明）+ 元数据块」—— 后者会被决策子代理判成系统通知而不回复。
#: 仍然只陈述事实，不带「请回复」这类催促语（那会让它句句都接）。
_LEAD_DIRECTED: dict[str, str] = {
    "at": "在频道里 @ 了你",
    "reply": "回复了你的留言",
    "comment": "在你的帖子下留了言",
    "dm": "私信你",
}
_LEAD_PASSIVE: dict[str, str] = {
    "like": "给你的帖子或留言点了赞",
    "favorite": "收藏了你的内容",
    "top": "顶了你的帖子",
    "system": "频道系统提示",
}
_LEAD_CHANNEL_COMMENT = "在频道里留了言"
_LEAD_CHANNEL_REPLY = "在楼中楼里说了话"
_LEAD_FEED = "在频道里发了新帖"

#: 平台通知的 ``summary`` 形如 ``评论了我的帖子:正文`` / ``回复了我:正文`` / ``赞了我的评论:``。
#: 前缀表达的信息已经由 ``type_label`` 与事实说明给出，留在正文里会变成
#: 「【QQ频道·收到评论】某某：评论了我的帖子:正文（评论了你的帖子）」这种重复且读起来别扭的文本，
#: 所以归一化时把前缀剥掉，只留真正的正文。
_SUMMARY_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"评论了(?:我的|你的|他的|她的)?帖子|"
    r"回复了我|回复了(?:我的|你的|他的|她的)?(?:评论|帖子)|"
    r"赞了(?:我的|你的|他的|她的)?(?:评论|帖子)?|"
    r"收藏了(?:我的|你的|他的|她的)?(?:评论|帖子)?|"
    r"顶了(?:我的|你的|他的|她的)?(?:评论|帖子)?|"
    r"@了我|提到了我|私信我|评论我|回复我|评论|回复"
    r")\s*[:：]\s*"
)


def strip_notice_prefix(text: Any) -> str:
    """去掉平台 ``summary`` 的动作前缀，只留正文（无前缀时原样返回）。"""
    body = _as_str(text)
    if not body:
        return ""
    return _SUMMARY_PREFIX_RE.sub("", body, count=1).strip()


def mentions_self(text: Any, names: Iterable[str]) -> bool:
    """本条正文里是否出现了本账号的昵称（按名字的强提及，不含 ``@``）。

    只用于判断**这一条内容自身**：帖子标题里的提及不应影响它下面评论的判定，
    否则「帖子点名了 bot」会让该帖所有评论都被当成在叫它（实测回复积极性过高）。
    """
    body = _as_str(text)
    if not body:
        return False
    folded = body.casefold()
    for name in names or ():
        candidate = _as_str(name).strip()
        if len(candidate) >= 2 and candidate.casefold() in folded:
            return True
    return False


# ── 宽容取值 ────────────────────────────────────────────────


def _find_deep(mapping: Mapping[str, Any], keys: Iterable[str], *, depth: int = 2) -> Any:
    """在 mapping 及其有限的嵌套 dict 容器里找第一个命中的键。"""
    keys = tuple(keys)
    for key in keys:
        if key in mapping:
            return mapping[key]
    if depth <= 0:
        return None
    for container in _NESTED_CONTAINERS:
        nested = mapping.get(container)
        if isinstance(nested, Mapping):
            found = _find_deep(nested, keys, depth=depth - 1)
            if found is not None:
                return found
    return None


def find_field(mapping: Mapping[str, Any], keys: Iterable[str], *, depth: int = 3) -> Any:
    """在任意嵌套结构里查找字段（公开版本，供 reply_sender 复用）。

    与 :func:`_find_deep` 不同，这里不限定容器名，而是对 dict / list 做有界深度优先搜索，
    适合处理 ``get-feed-detail`` / ``get-feed-comments`` 这类结构未知的返回。
    """
    if not isinstance(mapping, Mapping):
        return None
    keys = tuple(keys)
    for key in keys:
        if key in mapping:
            return mapping[key]
    if depth <= 0:
        return None
    for value in mapping.values():
        if isinstance(value, Mapping):
            found = find_field(value, keys, depth=depth - 1)
            if found is not None:
                return found
        elif isinstance(value, (list, tuple)):
            for item in list(value)[:20]:
                if isinstance(item, Mapping):
                    found = find_field(item, keys, depth=depth - 1)
                    if found is not None:
                        return found
    return None


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    if isinstance(value, Mapping):
        for key in ("user_id", "id", "tiny_id", "tinyId", "value", "text"):
            if key in value:
                return _as_str(value[key])
    return ""


#: 帖子/评论详情里「发送者昵称」的候选键（``feed get-feed-detail`` 的 ``author`` 等）
_DETAIL_NICK_KEYS = (
    "author",
    "author_nick",
    "authorNick",
    "poster_nick",
    "posterNick",
    "from_nick",
    "fromNick",
    "nick_name",
    "nickName",
    "nickname",
    "member_name",
    "memberName",
)
#: 详情里「发送者 ID」的候选键
_DETAIL_AUTHOR_ID_KEYS = (
    "author_id",
    "authorId",
    "poster_id",
    "posterId",
    "tiny_id",
    "tinyid",
    "tinyId",
    "from_tiny_id",
    "fromTinyId",
)


def find_detail_nickname(payload: Any) -> str:
    """从 ``get-feed-detail`` / ``get-feed-comments`` 的返回里取发送者昵称。

    通知本身经常不带昵称（渲染出来就是「未知用户」），只能到详情里补。
    纯数字结果视为 ID 而非昵称，返回空串。
    """
    if not isinstance(payload, Mapping):
        return ""
    raw = find_field(payload, _DETAIL_NICK_KEYS)
    if isinstance(raw, Mapping):
        text = _as_str(
            find_field(raw, ("nickname", "nick_name", "nickName", "nick", "name", "text"))
        )
    else:
        text = _as_str(raw)
    return "" if not text or text.isdigit() else text


def find_detail_author_id(payload: Any) -> str:
    """从详情里取发送者 tiny_id（可复用为 do-reply 需要的 author_id）。"""
    if not isinstance(payload, Mapping):
        return ""
    text = _as_str(find_field(payload, _DETAIL_AUTHOR_ID_KEYS))
    return text if text.isdigit() else ""


def _as_time(value: Any) -> Any:
    """时间字段原样保留（CLI 可能给秒级时间戳、毫秒时间戳或已格式化字符串）。"""
    if value is None or value == "":
        return ""
    return value


def category_of(type_raw: Any, *, has_comment: bool = False, has_reply: bool = False) -> str:
    """把通知类型（数值枚举 / NOTICE_TYPE_* 字符串 / 中文文案）归一到本插件的类别。"""
    if isinstance(type_raw, bool):
        type_raw = None
    if isinstance(type_raw, (int, float)) and not isinstance(type_raw, bool):
        return _NUMERIC_CATEGORY.get(int(type_raw), "unknown")
    text = _as_str(type_raw).upper()
    if text:
        if text.lstrip("-").isdigit():
            return _NUMERIC_CATEGORY.get(int(text), "unknown")
        for marker, category in _NAME_CATEGORY:
            if marker in text:
                return category
        if "私信" in _as_str(type_raw):
            return "dm"
        # 注意：真实通知的 type 是中文文案（"@我" / "回复" / "回复点赞" / "评论点赞" …），
        # 所以"点赞/收藏/顶帖"必须排在"回复/评论"**之前**判定——
        # 否则 "回复点赞" 会被 "回复" 抢先命中，点赞通知被错标成「收到回复」。
        if "赞" in _as_str(type_raw):
            return "like"
        if "收藏" in _as_str(type_raw):
            return "favorite"
        if "顶" in _as_str(type_raw):
            return "top"
        if "回复" in _as_str(type_raw):
            return "reply"
        if "评论" in _as_str(type_raw):
            return "comment"
        if "@" in _as_str(type_raw) or "提及" in _as_str(type_raw):
            return "at"
    if has_comment and has_reply:
        return "reply"
    if has_comment:
        return "comment"
    return "unknown"


def normalize_notice(raw: Mapping[str, Any], *, self_names: Iterable[str] = ()) -> dict[str, Any]:
    """把一条平台通知归一化成稳定字段（未知字段留空，不抛异常）。

    ``self_names`` 用于判断「本条正文是否点名了机器人」（强提及）；只在**本条**的
    正文/summary 上匹配，不掺入帖子标题。
    """
    raw = dict(raw or {})
    guild_id = _as_str(_find_deep(raw, _GUILD_KEYS))
    channel_id = _as_str(_find_deep(raw, _CHANNEL_KEYS))
    feed_id = _as_str(_find_deep(raw, _FEED_KEYS))
    comment_id = _as_str(_find_deep(raw, _COMMENT_KEYS))
    reply_id = _as_str(_find_deep(raw, _REPLY_KEYS))
    type_raw = _find_deep(raw, _TYPE_KEYS)
    # 幂等：入参已是本插件的归一化结构（带 category）时沿用它的结论，不重新猜类别。
    # 否则「评论轮询」合成出来的类别/中性标签会被降级成 unknown（directed_at_self 也会一起丢），
    # 注入文本就会退化成「其他人的评论，未提及你」。
    existing_category = _as_str(raw.get("category")).lower()
    if existing_category in _CATEGORY_LABEL:
        category = existing_category
    else:
        category = category_of(type_raw, has_comment=bool(comment_id), has_reply=bool(reply_id))

    create_time_raw = _as_time(_find_deep(raw, _CREATE_TIME_RAW_KEYS))
    create_time_text = _as_str(_find_deep(raw, _CREATE_TIME_TEXT_KEYS))

    from_user_id = _as_str(_find_deep(raw, _FROM_USER_KEYS))
    from_nick = _as_str(_find_deep(raw, _FROM_NICK_KEYS))
    content = _as_str(_find_deep(raw, _CONTENT_KEYS))
    # 通知的 content 就是平台 summary（形如「评论了我的帖子:正文」）：剥掉动作前缀，只留正文。
    # 只在「content 确实来自 summary」时剥，避免误伤真正的评论正文。
    summary = _as_str(raw.get("summary"))
    if summary and content == summary:
        content = strip_notice_prefix(content)
    notice_id = _as_str(_find_deep(raw, _ID_KEYS))

    if not notice_id:
        # 兜底：用可用字段拼一个稳定 id，避免同一通知被重复注入
        notice_id = "|".join(
            part
            for part in (
                "" if category == "unknown" else category,
                guild_id,
                feed_id,
                comment_id or reply_id,
                from_user_id,
                _as_str(create_time_raw) or create_time_text,
                content[:40],
            )
            if part
        )

    return {
        "id": notice_id,
        "category": category,
        "type_raw": type_raw,
        "type_label": _as_str(raw.get("type_label"))
        or _CATEGORY_LABEL.get(category, _CATEGORY_LABEL["unknown"]),
        #: 通知箱里的条目本来就来自机器人自己的帖子/评论 → 视为「在对它说话」；
        #: 若上游（评论轮询）已经判定过，则沿用它的结论
        "directed_at_self": (
            bool(raw.get("directed_at_self"))
            if "directed_at_self" in raw
            else category in _DIRECTED_CATEGORIES
        ),
        "mentioned_self": (
            bool(raw.get("mentioned_self"))
            if "mentioned_self" in raw
            else mentions_self(content, self_names)
        ),
        "actionable": category in ACTIONABLE_CATEGORIES,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "feed_id": feed_id,
        "feed_title": _as_str(_find_deep(raw, _FEED_TITLE_KEYS)),
        "feed_author_id": _as_str(_find_deep(raw, _FEED_AUTHOR_KEYS)),
        "feed_create_time": _as_time(_find_deep(raw, _FEED_CREATE_TIME_KEYS)),
        "feed_create_time_raw": _as_time(_find_deep(raw, _FEED_CREATE_TIME_RAW_KEYS)),
        "comment_id": comment_id,
        "comment_author_id": _as_str(_find_deep(raw, _COMMENT_AUTHOR_KEYS)),
        "comment_create_time": _as_time(_find_deep(raw, _COMMENT_CREATE_TIME_KEYS)),
        "reply_id": reply_id,
        "target_reply_id": _as_str(_find_deep(raw, _TARGET_REPLY_KEYS)),
        "target_user_id": _as_str(_find_deep(raw, _TARGET_USER_KEYS)),
        "from_user_id": from_user_id,
        "from_nickname": from_nick or from_user_id,
        "content": content,
        "create_time_raw": create_time_raw,
        "create_time_text": create_time_text,
        "source_guild_id": _as_str(_find_deep(raw, _SOURCE_GUILD_KEYS)),
        "raw": raw,
    }


def extract_feeds(payload: Any) -> list[dict[str, Any]]:
    """从 ``feed get-guild-feeds --json`` 的 ``data`` 里取出帖子列表。

    真实结构为 ``{"data": {"feeds": [...], "has_more": false}}``，这里同时兼容
    其它常见容器名。
    """
    if isinstance(payload, Mapping):
        for key in ("feeds", "feed_list", "feedList", "items", "list", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, Mapping)]
            if isinstance(value, Mapping):
                nested = extract_feeds(value)
                if nested:
                    return nested
        return []
    if isinstance(payload, (list, tuple)):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    return []


def notice_from_feed(
    feed: Mapping[str, Any],
    *,
    guild_id: str = "",
    self_names: Iterable[str] = (),
) -> dict[str, Any]:
    """把「频道帖子」转成与互动通知同构的记录。

    帖子本身不会产生互动通知（只有被 @/评论/回复才有），所以「新帖子」只能靠轮询
    帖子列表发现；转成同一结构后就能复用去重水位线、注入管线与出站回复逻辑。
    """
    feed_id = _as_str(find_field(feed, _FEED_KEYS))
    title = _as_str(find_field(feed, _FEED_TITLE_KEYS))
    snippet = _as_str(
        find_field(feed, ("content_snippet", "contentSnippet", "content", "text", "digest"))
    )
    content = snippet or title
    create_time_raw = _as_time(find_field(feed, _CREATE_TIME_RAW_KEYS))
    create_time_text = _as_str(find_field(feed, _CREATE_TIME_TEXT_KEYS))
    author_nick = find_detail_nickname(feed)
    author_id = find_detail_author_id(feed)

    return {
        "id": f"feed|{feed_id}" if feed_id else "",
        "category": "feed",
        "type_raw": "feed",
        "type_label": _CATEGORY_LABEL["feed"],
        #: 别人的新帖：频道动态，不是对机器人说的话（避免「看到就插话」）
        "directed_at_self": False,
        #: 但帖子正文里点名了机器人（如「示例Bot，看到请回复」）→ 属于真正的强提及
        "mentioned_self": mentions_self(content, self_names) or mentions_self(title, self_names),
        "actionable": True,
        "guild_id": _as_str(find_field(feed, _GUILD_KEYS)) or guild_id,
        "channel_id": _as_str(find_field(feed, _CHANNEL_KEYS)),
        "channel_name": _as_str(find_field(feed, ("channel_name", "channelName"))),
        "feed_id": feed_id,
        "feed_title": title,
        "feed_author_id": author_id,
        "feed_create_time": create_time_raw or create_time_text,
        "feed_create_time_raw": create_time_raw,
        "comment_id": "",
        "comment_author_id": "",
        "comment_create_time": "",
        "reply_id": "",
        "target_reply_id": "",
        "target_user_id": "",
        "from_user_id": author_id,
        "from_nickname": author_nick or author_id,
        "content": content,
        "create_time_raw": create_time_raw,
        "create_time_text": create_time_text,
        "source_guild_id": "",
        "raw": dict(feed),
    }


def canonical_notice_id(notice: Mapping[str, Any]) -> str:
    """统一的去重键（多条来源路径必须算出同一个键）。

    - 回复：``reply|<feed_id>|<reply_id>``
    - 评论：``comment|<feed_id>|<comment_id>``
    - 帖子：``feed|<feed_id>``
    - 其它（@ 通知、私信等）：沿用通知自带 id

    这样「互动通知」与「评论轮询」发现同一条评论/回复时只会注入一次。
    """
    feed_id = _as_str(notice.get("feed_id"))
    reply_id = _as_str(notice.get("reply_id"))
    comment_id = _as_str(notice.get("comment_id"))
    category = _as_str(notice.get("category")).lower()
    if reply_id:
        return f"reply|{feed_id}|{reply_id}"
    if comment_id:
        return f"comment|{feed_id}|{comment_id}"
    if category == "feed" and feed_id:
        return f"feed|{feed_id}"
    return _as_str(notice.get("id"))


def extract_comments(payload: Any) -> list[dict[str, Any]]:
    """从 ``feed get-feed-comments --json`` 的 ``data`` 里取出评论列表。

    真实结构为 ``{"data": {"comments": [...], "has_more": bool}}``；
    无评论时该键直接不存在，这里一律返回空列表。
    """
    if isinstance(payload, Mapping):
        for key in ("comments", "comment_list", "commentList", "items", "list", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, Mapping)]
            if isinstance(value, Mapping):
                nested = extract_comments(value)
                if nested:
                    return nested
        return []
    if isinstance(payload, (list, tuple)):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    return []


def extract_replies(comment: Mapping[str, Any]) -> list[dict[str, Any]]:
    """取出一条评论下预加载的回复（``--reply-list-num``）。

    实测（tencent-channel-cli 1.0.10）真实字段名是 **``replies_preview``**；
    早先只认 ``replies`` / ``reply_list`` / ``replyList``，导致楼中楼回复
    （``某位成员`` 那类）永远进不来。这里把三个名字都收进来，顺序按真实字段优先。
    """
    value = find_field(
        comment, ("replies_preview", "repliesPreview", "replies", "reply_list", "replyList")
    )
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _comment_body(item: Mapping[str, Any]) -> str:
    """取评论/回复的纯文本正文（顺带剥掉平台 summary 前缀）。"""
    return strip_notice_prefix(
        find_field(item, ("content_text", "contentText", "text", "content"))
    )


def match_comment_by_body(
    comments: Iterable[Mapping[str, Any]], body: str
) -> Mapping[str, Any] | None:
    """在帖子的评论（含预加载的楼中楼）里按**正文**找唯一匹配的一条。

    用途：平台的互动通知**不带 comment_id**（只有 ``summary``），所以「谁评论了我的帖子」
    只能靠正文回查 —— 绝不能拿帖子作者顶替：实测那条帖子的作者就是 bot 自己，
    于是「某位成员评论了你的帖子」被渲染成「示例Bot：评论了我的帖子…」。

    匹配规则：先精确相等，再退化为互相包含（双方都 ≥4 字）。命中**多个不同作者**时返回 None
    （宁可不猜、渲染成「未知用户」，也不安到错的人头上）。
    """
    target = _as_str(body).strip()
    if not target:
        return None

    candidates: list[Mapping[str, Any]] = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        candidates.append(comment)
        candidates.extend(extract_replies(comment))

    def _author_of(item: Mapping[str, Any]) -> str:
        return find_detail_author_id(item) or _as_str(
            find_field(item, ("author", "nickname", "nick_name"))
        )

    exact: dict[str, Mapping[str, Any]] = {}
    loose: dict[str, Mapping[str, Any]] = {}
    for item in candidates:
        text = _comment_body(item).strip()
        if not text:
            continue
        author = _author_of(item)
        if not author:
            continue
        if text == target:
            exact.setdefault(author, item)
        elif len(target) >= 4 and len(text) >= 4 and (target in text or text in target):
            loose.setdefault(author, item)

    for bucket in (exact, loose):
        if len(bucket) == 1:
            return next(iter(bucket.values()))
    return None


def _comment_at_self(item: Mapping[str, Any], self_tiny_id: str) -> bool:
    """该评论/回复是否 @ 了本账号。"""
    if not self_tiny_id:
        return False
    at_users = find_field(item, ("at_users", "atUsers"))
    if not isinstance(at_users, list):
        return False
    for entry in at_users:
        if not isinstance(entry, Mapping):
            continue
        ids = {_as_str(entry.get("id")), _as_str(entry.get("tiny_id")), _as_str(entry.get("tinyid"))}
        if self_tiny_id in ids:
            return True
    return False


def notice_from_comment(
    item: Mapping[str, Any],
    *,
    feed_id: str,
    guild_id: str = "",
    feed_title: str = "",
    self_tiny_id: str = "",
    kind: str = "comment",
    parent_comment_id: str = "",
    parent_author_id: str = "",
    self_names: Iterable[str] = (),
) -> dict[str, Any]:
    """把「帖子评论 / 楼中楼回复」转成与互动通知同构的记录。

    用于「评论轮询」：把帖子评论区里的对话（包括别人之间的对话）也注入给 LLM，
    这样频道才会像群聊一样有连续的上下文。

    **是否算「在对机器人说话」**（决定注入文本里的事实说明，直接影响回复积极性）：

    - 评论/回复里 ``@`` 了本账号 → 是（类别 ``at``）
    - 楼中楼回复，且**父评论作者是本账号** → 是（类别 ``reply``）
    - 其它情况（别人之间的对话）→ **不是**：类别沿用 ``comment`` / ``reply``（保持
      ``channel.notices.types`` 过滤语义不变），但 ``directed_at_self=False``，
      标签改成中性的「频道评论 / 频道楼中楼」。

    Args:
        item: 评论或回复对象（``feed get-feed-comments`` 的返回元素）。
        kind: ``comment``（一级评论）或 ``reply``（楼中楼回复）。
        parent_comment_id: ``kind=reply`` 时所属的一级评论 ID。
        parent_author_id: ``kind=reply`` 时所属一级评论的作者 tiny_id（用来判断是不是在回复机器人）。
    """
    is_reply = kind == "reply"
    item_id = _as_str(find_field(item, _REPLY_KEYS if is_reply else _COMMENT_KEYS))
    if not item_id:
        item_id = _as_str(find_field(item, _COMMENT_KEYS if is_reply else _REPLY_KEYS))

    author_nick = find_detail_nickname(item)
    author_id = find_detail_author_id(item)
    text = _as_str(find_field(item, ("content_text", "contentText", "text", "content")))
    create_raw = _as_time(find_field(item, _CREATE_TIME_RAW_KEYS))
    create_text = _as_str(find_field(item, _CREATE_TIME_TEXT_KEYS))
    at_self = _comment_at_self(item, self_tiny_id)
    directed = at_self or (
        is_reply and bool(self_tiny_id) and _as_str(parent_author_id) == self_tiny_id
    )
    mentioned = mentions_self(text, self_names)

    if directed:
        category = "at" if at_self else "reply"
        label_key = category
    else:
        # 过滤语义沿用 comment/reply（channel.notices.types 不用改），
        # 但标签与事实说明按「别人之间的对话」渲染
        category = "reply" if is_reply else "comment"
        label_key = "conversation_reply" if is_reply else "conversation"
    if is_reply:
        notice_id = f"reply|{feed_id}|{item_id}"
    else:
        notice_id = f"comment|{feed_id}|{item_id}"

    return {
        "id": notice_id if item_id else "",
        "category": category,
        "type_raw": category,
        "type_label": _CATEGORY_LABEL[label_key],
        "directed_at_self": directed,
        #: 本条正文里出现了机器人昵称（强提及）：只按本条内容算，不带帖子标题
        "mentioned_self": mentioned,
        "actionable": True,
        "guild_id": guild_id,
        "channel_id": "",
        "feed_id": feed_id,
        "feed_title": feed_title,
        "feed_author_id": "",
        "feed_create_time": "",
        "feed_create_time_raw": "",
        "comment_id": item_id if not is_reply else parent_comment_id,
        # 楼中楼回复的出站目标是「父评论」：评论级作者/时间必须描述父评论，而不是这条回复本身。
        # 这里对 reply 一律留空，交给 reply_sender.enrich_comment_meta 按 comment_id 补真实值
        # （若把回复自己的时间填进去，enrich 会因「已是合法秒级时间戳」而不再纠正）。
        "comment_author_id": author_id if not is_reply else "",
        "comment_create_time": (create_raw or create_text) if not is_reply else "",
        "reply_id": item_id if is_reply else "",
        # 出站语义与 CLI 返回里同名字段对齐：
        # target_reply_id = 被回复的那条消息（这条楼中楼回复自己），target_user_id = 它的作者。
        "target_reply_id": item_id if is_reply else "",
        "target_user_id": author_id if is_reply else "",
        "from_user_id": author_id,
        "from_nickname": author_nick or author_id,
        "content": text,
        "create_time_raw": create_raw,
        "create_time_text": create_text,
        "source_guild_id": guild_id,
        "raw": dict(item),
    }


def extract_notices(payload: Any) -> list[dict[str, Any]]:
    """从 ``feed get-notices --json`` 的 ``data`` 里取出通知列表。

    真实 payload 结构未文档化，这里覆盖常见容器；都没有时，
    若 payload 本身像一条通知则当作单条处理。
    """
    if payload is None:
        return []
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []

    containers = (
        "notices",
        "notice_list",
        "noticeList",
        "new_notices",
        "newNotices",
        "new_interact_notices",
        "items",
        "list",
        "records",
        "data",
        "result",
    )
    for key in containers:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            nested = extract_notices(value)
            if nested:
                return nested

    if any(key in payload for key in ("notice_id", "noticeId", "feed_id", "feedId")):
        return [dict(payload)]
    return []


def sort_notices(notices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按时间升序排列（拿不到时间的排在前面，保持稳定）。"""

    def sort_key(notice: Mapping[str, Any]) -> float:
        value = notice.get("create_time_raw") or notice.get("feed_create_time_raw") or 0
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if number > 1e11:  # 毫秒
            number /= 1000.0
        return number

    return sorted(notices, key=sort_key)


# ── 路由键 ──────────────────────────────────────────────────


def _sanitize(part: Any) -> str:
    return _as_str(part).replace("|", "_").replace("\n", " ")


def build_group_id(guild_id: str, feed_id: str, comment_id: str = "") -> str:
    """构造路由用 group_id。"""
    parts = [ROUTE_PREFIX, _sanitize(guild_id), _sanitize(feed_id)]
    if _sanitize(comment_id):
        parts.append(_sanitize(comment_id))
    return "|".join(parts)


def build_channel_group_id(guild_id: str) -> str:
    """频道级会话 key：**整个频道当一个群**（``tcf|<guild_id>``）。

    与按帖子分会话（``tcf|<guild>|<feed>[|<comment>]``）相对；开启后所有帖子与
    评论都进同一条会话流，出站目标由 Adapter 的「待回复队列」决定。
    """
    return "|".join([ROUTE_PREFIX, _sanitize(guild_id)])


def parse_group_id(group_id: str) -> dict[str, str] | None:
    """反解路由 group_id（频道级 ``tcf|<guild>`` 也认，feed/comment 为空）。"""
    parts = _as_str(group_id).split("|")
    if len(parts) < 2 or parts[0] != ROUTE_PREFIX:
        return None
    return {
        "guild_id": parts[1] if len(parts) > 1 else "",
        "feed_id": parts[2] if len(parts) > 2 else "",
        "comment_id": parts[3] if len(parts) > 3 else "",
    }


def stream_id_for_key(platform: str, key: str, *, private: bool = False) -> str:
    """复刻 ``ChatStream.generate_stream_id``（便于日志/调试定位）。"""
    if private:
        raw = f"{platform}_{key}_private"
    else:
        raw = f"{platform}_{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class NoticeRoute:
    """一条通知对应的会话路由与出站上下文。"""

    kind: str  # "group" | "private"
    key: str  # group_id 或 user_id
    label: str
    ctx: dict[str, Any] = field(default_factory=dict)
    stream_id: str = ""


def route_for_notice(
    notice: Mapping[str, Any],
    *,
    platform: str,
    guild_id_fallback: str = "",
    channel_as_group: bool = False,
) -> NoticeRoute:
    """根据归一化通知决定会话路由与出站上下文。

    - 有 ``feed_id``：群聊
      - 默认 key = ``tcf|guild|feed[|comment]``（每条帖子 / 每条评论各一条会话）
      - ``channel_as_group=True`` 时 key = ``tcf|guild``（**整个频道当一个群**，
        所有帖子与评论共用一条会话，出站目标由适配器的待回复队列决定）
    - 无 ``feed_id``（私信）：私聊，key = ``from_user_id``
    """
    guild_id = _as_str(notice.get("guild_id")) or _as_str(guild_id_fallback)
    feed_id = _as_str(notice.get("feed_id"))
    comment_id = _as_str(notice.get("comment_id"))
    category = _as_str(notice.get("category")) or "unknown"

    ctx: dict[str, Any] = {
        "notice_id": _as_str(notice.get("id")),
        "category": category,
        "guild_id": guild_id,
        "channel_id": _as_str(notice.get("channel_id")),
        "feed_id": feed_id,
        "feed_title": _as_str(notice.get("feed_title")),
        "feed_author_id": _as_str(notice.get("feed_author_id")),
        "feed_create_time": notice.get("feed_create_time") or "",
        "feed_create_time_raw": notice.get("feed_create_time_raw") or "",
        "comment_id": comment_id,
        "comment_author_id": _as_str(notice.get("comment_author_id")),
        "comment_create_time": notice.get("comment_create_time") or "",
        "reply_id": _as_str(notice.get("reply_id")),
        "target_reply_id": _as_str(notice.get("target_reply_id")),
        "target_user_id": _as_str(notice.get("target_user_id")),
        "from_user_id": _as_str(notice.get("from_user_id")),
        "from_nickname": _as_str(notice.get("from_nickname")),
        "source_guild_id": _as_str(notice.get("source_guild_id")) or guild_id,
        "received_at": time.time(),
    }

    if feed_id:
        if channel_as_group:
            # 整个频道当一个群：所有帖子/评论共用一个会话键
            key = build_channel_group_id(guild_id)
            label = f"频道 {guild_id}"
        else:
            key = build_group_id(guild_id, feed_id, comment_id)
            label = f"帖子 {_as_str(notice.get('feed_title')) or feed_id}"
            if comment_id:
                label += f" / 评论 {comment_id}"
        return NoticeRoute(
            kind="group",
            key=key,
            label=label,
            ctx=ctx,
            stream_id=stream_id_for_key(platform, key),
        )

    peer = _as_str(notice.get("from_user_id")) or _as_str(notice.get("target_user_id"))
    return NoticeRoute(
        kind="private",
        key=peer,
        label=f"私信 {_as_str(notice.get('from_nickname')) or peer}",
        ctx=ctx,
        stream_id=stream_id_for_key(platform, peer, private=True),
    )


def notice_text(notice: Mapping[str, Any]) -> str:
    """渲染给 LLM 看的通知文本 —— **写成「某人说了一句话」，不是系统通知**。

    为什么是现在这个样子（三个坑，按时间顺序）：

    1. 早期对 ``at/reply/dm`` 追加「（这条在跟你说话）」这类催促语 → 回复积极性过高；
    2. 接着把评论注入文本里的**帖子标题/正文**去掉（只留帖子 ID）→ 否则帖名里的点名
       会让该帖每条评论都被判成在叫它；
    3. 但 ``【QQ频道·收到评论】<昵称>：<正文>（评论了你的帖子）`` 加 ``所属帖子ID：…``
       ``时间：…`` 这组「平台标签 + 元数据块」，会被**决策子代理**判成
       「QQ频道系统的通知消息，并非直接对机器人发起的对话」而拒绝回复
       （真实日志：``08:17:09 default_chatter | 子代理决策: …属于系统通知而非互动消息 (respond=False)``）。

    所以最终形态：把**关系**写成自然语言（「在你的帖子下留了言」），
    **不带平台标签、不带元数据块、不重复昵称** —— 昵称和时间由框架的消息行给出
    （``【时间】[平台ID] 昵称:名字 [msg_id]： 内容``，昵称来自 envelope 的
    ``user_info.user_nickname``）。非指向机器人的对话仍保留 ``（未提及你）`` 这类**事实**，
    避免又回到「句句都接」。
    """
    content = _as_str(notice.get("content"))
    category = _as_str(notice.get("category")).lower()
    directed = (
        bool(notice.get("directed_at_self"))
        if "directed_at_self" in notice
        else category in _DIRECTED_CATEGORIES
    )
    mentioned = bool(notice.get("mentioned_self"))

    if directed:
        lead = _LEAD_DIRECTED.get(category, "提到了你")
        suffix = ""
    elif category in _LEAD_PASSIVE:
        lead = _LEAD_PASSIVE[category]
        suffix = ""
    else:
        if category == "feed":
            lead = _LEAD_FEED
        elif _as_str(notice.get("reply_id")):
            lead = _LEAD_CHANNEL_REPLY
        else:
            lead = _LEAD_CHANNEL_COMMENT
        suffix = "（提到了你）" if mentioned else "（未提及你）"

    if content:
        return f"{lead}{suffix}：{content}"
    return f"{lead}{suffix}"


def envelope_for_notice(
    notice: Mapping[str, Any],
    route: NoticeRoute,
    *,
    platform: str,
    message_prefix: str = "",
) -> dict[str, Any]:
    """构造入站 ``MessageEnvelope``（dict 形态，兼容 mofox_wire）。

    刻意不使用 ``MessageBuilder``：其默认 ``direction`` 是 ``"outgoing"``，
    且单段/多段形态不一致，直接构造 dict 更可控。
    """
    text = notice_text(notice)
    if message_prefix:
        text = f"{message_prefix}{text}"

    create_time = notice.get("create_time_raw")
    try:
        timestamp = float(create_time)
    except (TypeError, ValueError):
        timestamp = time.time()
    if timestamp > 1e11:  # 毫秒 → 秒
        timestamp /= 1000.0

    message_info: dict[str, Any] = {
        "platform": platform,
        "message_id": _as_str(notice.get("id")) or f"tc_{int(time.time() * 1000)}",
        "time": timestamp,
        "user_info": {
            "platform": platform,
            "user_id": route.ctx.get("from_user_id") or route.key or "unknown_user",
        },
        # 运行时扩展字段：框架会把它合并进 Message.extra（不要指望它进**出站** envelope）
        "extra": {
            "tencent_channel": {k: v for k, v in route.ctx.items() if isinstance(v, (str, int, float))},
            "notice_category": _as_str(notice.get("category")),
            #: 这条是不是「在对机器人说话」：供 chatter/框架侧按需过滤（true = @我/回复我的评论/私信）
            "notice_directed": bool(notice.get("directed_at_self")),
            #: 本条**正文**里是否点名了机器人（不含帖子标题里的提及）
            "notice_mentioned": bool(notice.get("mentioned_self")),
            "route_key": route.key,
            "route_kind": route.kind,
        },
    }
    if _as_str(notice.get("from_nickname")):
        message_info["user_info"]["user_nickname"] = _as_str(notice.get("from_nickname"))
    if route.kind == "group":
        message_info["group_info"] = {
            "platform": platform,
            "group_id": route.key,
            "group_name": route.label,
        }
    message_info["format_info"] = {"content_format": ["text"], "accept_format": ["text"]}

    return {
        "direction": "incoming",
        "message_info": message_info,
        "message_segment": [{"type": "text", "data": text}],
        "raw_message": notice.get("raw") or dict(notice),
    }


def extract_text(envelope: Mapping[str, Any]) -> str:
    """从 envelope 的 ``message_segment`` 里取纯文本（出站用）。"""
    segment = envelope.get("message_segment")
    if isinstance(segment, Mapping):
        segments = [segment]
    elif isinstance(segment, (list, tuple)):
        segments = [seg for seg in segment if isinstance(seg, Mapping)]
    else:
        return ""
    parts: list[str] = []
    for seg in segments:
        if _as_str(seg.get("type")) == "text":
            parts.append(_as_str(seg.get("data")))
    return "".join(parts).strip()


def extract_target(message_info: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """从出站 ``message_info`` 反解目标。

    Returns:
        ``(kind, key, parsed_group)``：kind ∈ ``group`` / ``private`` / ``unknown``
    """
    message_info = message_info or {}
    group_info = message_info.get("group_info")
    if isinstance(group_info, Mapping):
        group_id = _as_str(group_info.get("group_id"))
        if group_id:
            return "group", group_id, (parse_group_id(group_id) or {})
    user_info = message_info.get("user_info")
    if isinstance(user_info, Mapping):
        user_id = _as_str(user_info.get("user_id"))
        if user_id:
            return "private", user_id, {}
    return "unknown", "", {}


# ── 去重 / 水位线 ───────────────────────────────────────────


class NoticeWatermark:
    """按「已见 id 集合 + 时间水位线」去重，避免重复注入/重复回复。"""

    def __init__(self, *, max_seen: int = 2000) -> None:
        self.max_seen = max(1, int(max_seen))
        self._seen: list[str] = []
        self._seen_set: set[str] = set()
        self.watermark: float = 0.0

    # -- 序列化（可选持久化）--
    def to_dict(self) -> dict[str, Any]:
        return {"watermark": self.watermark, "seen": list(self._seen)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> NoticeWatermark:
        wm = cls()
        if not isinstance(data, Mapping):
            return wm
        try:
            wm.watermark = float(data.get("watermark") or 0.0)
        except (TypeError, ValueError):
            wm.watermark = 0.0
        seen = data.get("seen")
        if isinstance(seen, list):
            for item in seen:
                wm._remember(_as_str(item))
        return wm

    # -- 判定 --
    def _remember(self, notice_id: str) -> None:
        if not notice_id or notice_id in self._seen_set:
            return
        self._seen.append(notice_id)
        self._seen_set.add(notice_id)
        while len(self._seen) > self.max_seen:
            dropped = self._seen.pop(0)
            self._seen_set.discard(dropped)

    @staticmethod
    def _time_of(notice: Mapping[str, Any]) -> float:
        value = notice.get("create_time_raw")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return number / 1000.0 if number > 1e11 else number

    def is_new(self, notice: Mapping[str, Any]) -> bool:
        """是否为新通知（只判断，不记账）。"""
        notice_id = _as_str(notice.get("id"))
        if notice_id and notice_id in self._seen_set:
            return False
        notice_time = self._time_of(notice)
        if notice_time and self.watermark and notice_time < self.watermark:
            return False
        if not notice_id and not notice_time:
            return False
        return True

    def mark(self, notice: Mapping[str, Any]) -> None:
        """记账。"""
        self._remember(_as_str(notice.get("id")))
        notice_time = self._time_of(notice)
        self.watermark = max(self.watermark, notice_time)

    def filter_new(
        self,
        notices: Iterable[Mapping[str, Any]],
        *,
        mark: bool = True,
    ) -> list[dict[str, Any]]:
        """返回新通知（默认同时记账），保持时间升序。"""
        fresh: list[dict[str, Any]] = []
        for notice in sort_notices([dict(n) for n in notices]):
            if self.is_new(notice):
                fresh.append(notice)
            if mark:
                self.mark(notice)
        return fresh

    @property
    def seen_count(self) -> int:
        return len(self._seen)


def is_actionable(notice: Mapping[str, Any], allowed: Iterable[str] | None = None) -> bool:
    """该通知是否应该注入给 LLM（点赞/收藏/顶帖默认不注入，避免刷屏）。"""
    category = _as_str(notice.get("category")) or "unknown"
    allowed_set = {str(item).strip().lower() for item in (allowed or ACTIONABLE_CATEGORIES)}
    return category.lower() in allowed_set
