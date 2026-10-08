"""网关直连客户端（``cli.mode = "gateway"``）：不经 CLI 子进程，插件直接调 MCP 网关。

协议（2026-10-02 用 CLI debug 日志 ``mcp request body`` + 真机探针逆向，已实测）：

- **端点**：``https://graph.qq.com/mcp_gateway/open_platform_agent_mcp/mcp``，
  JSON-RPC 2.0，**无状态**（无 initialize 握手，每次调用独立）；
- **请求**：``{"jsonrpc":"2.0","id":N,"method":"tools/call",
  "params":{"name":"<工具名>","arguments":{...}}}``，鉴权 ``Authorization: Bearer <token>``；
- **响应**：业务数据在 ``result.structuredContent``，业务码在
  ``result._meta.AdditionalFields.retCode``；失败时 ``result.isError=true`` 且
  ``errMsg`` 带说明（HTTP 仍可能是 200）。HTTP 层失败（401 等）返回
  ``{"error":{"code","message"}}`` 形态。

工具名是蛇形（``get_guild_info``），参数名**大多是驼峰**（``guildId``）且与 CLI
flag 不一一对应（``--count`` 在 get-feed-comments 里是 ``listNum``、
``--rank-type`` 是 ``rankingType``，还需 ``channelSign``），个别内层结构甚至是
蛇形（``channelSign.channel_id``）。下表 12 条映射全部来自真机捕获（CLI ``-v``
日志或直连探针 retCode=0）；**未列入的命令走通用规则**（工具名 = action 蛇形、
参数名 snake→camel），其中写命令未实测，失败会被网关的参数校验报错指路
（形如 ``Error at "/guildIds": property ... is missing``）。

与 CLI 模式的关系：``GatewayClient`` 继承 ``TencentChannelCli``，方法面完全一致，
``cli_service.build_cli_client`` 按 ``cli.mode`` 二选一；CLI 模式仍是默认。
gateway 模式**不做扫码登录**（设备授权协议在 CLI 里，未逆向）——令牌用
``cli.login.token_file`` 提供，或本机凭据存储里已有 CLI 登录过的令牌
（token_login 直接读得到，不需要 CLI 进程在场）。

运维须知（2026-10-02 全链路真机验证后）：

- 读 6 条（频道解析/通知/新帖/评论/详情/用户）+ 写 6 条（评论/楼中楼/帖子赞/
  评论赞/发帖/私信）的参数映射与输出归一化全部真机验证；私信受 QQ 规则限制
  （100707：对方回复前只能发 1 条），与 CLI 模式一致。
- 该网关是 QQ AI Connect 的内部 MCP 口子，非公开 API；直连的 Header/UA 与 CLI
  略有差异，理论上有被服务端风控的可能。CLI 模式（厂商客户端）仍是默认。
- 扫码授权（首次获取令牌）仍依赖 CLI；令牌经 ``cli.login.token_file`` 预置后，
  全新设备无需安装 CLI 即可运行全部功能。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .cli_client import CliResult, TencentChannelCli, classify

DEFAULT_GATEWAY_ENDPOINT = "https://graph.qq.com/mcp_gateway/open_platform_agent_mcp/mcp"

#: 与 CLI 捕获一致的字段掩码（缺了会 8010/36003，照抄 CLI 的请求）
_GUILD_INFO_FILTER = {
    "info": dict.fromkeys(("uint32CreateTime", "uint32FaceSeq", "uint32GuildName", "uint32GuildNumber", "uint32MemberNum", "uint32Profile", "uint32VistorInteractionAllSwitch"), 1)
}
_USER_MSG_FILTER = dict.fromkeys(("uint32City", "uint32Country", "uint32Gender", "uint32IsGuildAuthor", "uint32MemberName", "uint32NickName", "uint32Province"), 1)


def _snake_to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


# ── 实测映射表（真机捕获，勿凭记忆改）─────────────────────────


def _args_get_interact_notice(p: Mapping[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if p.get("guild_id"):
        args["guildId"] = p["guild_id"]
    args["pageNum"] = int(p.get("page_num") or 20)
    return args


def _args_get_guild_feeds(p: Mapping[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {
        "guildId": p.get("guild_id", ""),
        "getType": int(p.get("get_type") or 2),
        "count": int(p.get("count") or 10),
    }
    if p.get("feed_cookie"):
        args["feedCookie"] = p["feed_cookie"]
    return args


def _channel_sign(p: Mapping[str, Any]) -> dict[str, Any]:
    """构造 ``channelSign``：**只放非空字段，空值整个省略**。

    网关的 proto 里这两个键是 uint64：传空串会直接
    ``8004 jsonpb.UnmarshalString failed: strconv.ParseUint: parsing "": invalid syntax``
    （真机实测踩过：``channel.channel_id`` 没填时评论轮询每轮都失败）。
    实测只带 ``guild_id``（省略 ``channel_id``）是能成功的，所以这里空值不写。
    """
    sign: dict[str, Any] = {}
    if str(p.get("channel_id") or "").strip():
        sign["channel_id"] = str(p["channel_id"])
    if str(p.get("guild_id") or "").strip():
        sign["guild_id"] = str(p["guild_id"])
    return sign


def _args_get_feed_comments(p: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "feedId": p.get("feed_id", ""),
        "listNum": int(p.get("count") or 20),
        "rankingType": int(p.get("rank_type") or 2),
        "replyListNum": int(p.get("reply_list_num") or 0),
        "channelSign": _channel_sign(p),
        "render_sticker": True,
    }


def _args_get_feed_detail(p: Mapping[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"feedId": p.get("feed_id", "")}
    if p.get("guild_id"):
        args["guildId"] = p["guild_id"]
    return args


def _args_get_user_info(p: Mapping[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"msgFilter": dict(_USER_MSG_FILTER)}
    if p.get("guild_id"):
        args["guildId"] = p["guild_id"]
    if p.get("tiny_id"):
        args["uint64MemberTinyid"] = p["tiny_id"]
    return args


def _args_get_guild_info(p: Mapping[str, Any]) -> dict[str, Any]:
    return {"reqGuildInfos": [{"guildId": p.get("guild_id", "")}], "filter": dict(_GUILD_INFO_FILTER)}


def _args_get_guild_channel_list(p: Mapping[str, Any]) -> dict[str, Any]:
    return {"guildIds": [p.get("guild_id", "")]}


def _args_get_share_info(p: Mapping[str, Any]) -> dict[str, Any]:
    return {"shortUrl": p.get("url", "")}


def _args_guild_member_search(p: Mapping[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {
        "guildId": p.get("guild_id", ""),
        "keyword": p.get("keyword", ""),
        "num": int(p.get("num") or 20),
        "pos": "0",
        "sourceId": "ALL_MEMBER_LIST",
        "fillOption": {"avatar": False},
    }
    return args


def _args_get_share_url(p: Mapping[str, Any]) -> dict[str, Any]:
    return {"guildId": p.get("guild_id", ""), "isShortLink": True}


def _b64(value: Any) -> str:
    """解码网关的 ``bytes*`` 字段（base64 → UTF-8；空/坏值返回空串）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return base64.b64decode(text).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 个别字段可能不是 base64，原样返回
        return text


def _args_search_guild_content(p: Mapping[str, Any]) -> dict[str, Any]:
    # 形态取自 CLI -v 捕获；scope 只有一种已验证取值，tab/tabMask 固定照抄
    return {
        "keyWord": p.get("keyword", ""),
        "tab": {"tabMask": "3"},
        "channelConditionFilter": {"rankType": "CHANNEL_RANK_TYPE_SMART"},
        "disableCorrectionQuery": False,
        "sessionInfo": "",
    }


def _args_do_comment(p: Mapping[str, Any]) -> dict[str, Any]:
    """do-comment 的嵌套形态（真机捕获）：comment/feed/jsonComment 三段组装。

    ``createTime`` 用当前秒级时间戳；``feed.createTime`` 是被评论帖子的
    创建时间（缺了网关报 8010 createTime 格式不正确——本机首测踩过）。
    """
    content = str(p.get("content") or "")
    feed_ct = p.get("feed_create_time")
    if isinstance(feed_ct, (int, float)) and feed_ct:
        feed_ct_str = str(int(feed_ct))
    else:
        feed_ct_str = str(feed_ct or "").strip()
    return {
        "comment": {"content": content, "createTime": str(int(time.time())), "postUser": {"id": ""}},
        "commentType": int(p.get("comment_type") or 1),
        "feed": {
            "channelInfo": {"sign": _channel_sign(p)},
            "createTime": feed_ct_str,
            "id": str(p.get("feed_id") or ""),
            "poster": {"id": ""},
        },
        "jsonComment": json.dumps(
            {"contents": [{"pattern_id": "", "text_content": {"text": content}, "type": 1}]},
            ensure_ascii=False,
        ),
    }


def _args_do_feed_prefer(p: Mapping[str, Any]) -> dict[str, Any]:
    args = {"action": int(p.get("action") or 1), "feedId": str(p.get("feed_id") or "")}
    if p.get("guild_id"):
        args["guildId"] = str(p["guild_id"])
    if p.get("channel_id"):
        args["channelId"] = str(p["channel_id"])
    return args


def _args_do_reply(p: Mapping[str, Any]) -> dict[str, Any]:
    """do-reply 的嵌套形态（真机捕获）：与 do-comment 同构，comment 段带被回复评论的上下文。"""
    content = str(p.get("content") or "")
    feed_ct = p.get("feed_create_time")
    feed_ct_str = str(int(feed_ct)) if isinstance(feed_ct, (int, float)) and feed_ct else str(feed_ct or "").strip()
    comment_ct = p.get("comment_create_time")
    comment_ct_str = str(int(comment_ct)) if isinstance(comment_ct, (int, float)) and comment_ct else str(comment_ct or "").strip()
    return {
        "comment": {
            "createTime": comment_ct_str,
            "id": str(p.get("comment_id") or ""),
            "postUser": {"id": str(p.get("comment_author_id") or "")},
        },
        "feed": {
            "channelInfo": {"sign": _channel_sign(p)},
            "createTime": feed_ct_str,
            "id": str(p.get("feed_id") or ""),
            "poster": {"id": str(p.get("feed_author_id") or "")},
        },
        "jsonReply": json.dumps(
            {"contents": [{"pattern_id": "", "text_content": {"text": content}, "type": 1}]},
            ensure_ascii=False,
        ),
        "reply": {"postUser": {"id": str(p.get("replier_id") or "")}},
        "replyType": int(p.get("reply_type") or 1),
    }


def _args_do_like(p: Mapping[str, Any]) -> dict[str, Any]:
    """do-like 的嵌套形态（真机捕获）：like_type 3/4=评论赞/取消，5/6=回复赞/取消。"""
    like_type = int(p.get("like_type") or 3)
    target_id = str(p.get("reply_id") or p.get("comment_id") or "")
    author_id = str(p.get("reply_author_id") or p.get("comment_author_id") or "")
    feed_ct = p.get("feed_create_time")
    feed_ct_str = str(int(feed_ct)) if isinstance(feed_ct, (int, float)) and feed_ct else str(feed_ct or "").strip()
    return {
        "comment": {
            "id": str(p.get("comment_id") or target_id),
            "likeInfo": {"count": 0, "id": target_id, "status": 1 if like_type in (3, 5) else 0},
            "postUser": {"id": author_id},
        },
        "feed": {
            "channelInfo": {"sign": _channel_sign(p)},
            "createTime": feed_ct_str,
            "id": str(p.get("feed_id") or ""),
            "poster": {"id": str(p.get("feed_author_id") or "")},
        },
        "like": {"id": target_id, "status": 1 if like_type in (3, 5) else 0},
        "likeType": like_type,
    }


def _args_publish_feed(p: Mapping[str, Any]) -> dict[str, Any]:
    """publish-feed 的嵌套形态（真机捕获）：feed 段 + 大骨架 jsonFeed。

    patternInfo/块 ID 的 UUID 由 CLI 现生成，这里同样现生成；已验证路径为
    纯文本帖（feed_type=1 → jsonFeed.feed_type=2，照抄 CLI 的内部映射）。
    """
    content = str(p.get("content") or "")
    title = str(p.get("title") or "")
    now_ms = int(time.time() * 1000)
    block = {"pattern_id": "", "text_content": {"text": content}, "type": 1}

    def _para(text: str, block_id: str) -> dict[str, Any]:
        return {"data": [{"props": {"fontWeight": 400, "italic": False, "underline": False}, "text": text, "type": 1}],
                "id": block_id, "props": {"textAlignment": 0}, "type": "blockParagraph"}

    pattern_info = [
        {"data": [{"children": [], "text": "", "type": 1}], "id": str(uuid.uuid4()).upper(), "type": "blockParagraph"},
        _para(content, str(now_ms)),
    ]
    json_feed = {
        "at_users": None,
        "channelInfo": {"is_square": True, "name": "",
                        "sign": {**_channel_sign(p), "channel_type": 0}},
        "client_task_id": str(uuid.uuid4()).upper(),
        "contents": {"contents": [block]},
        "createTime": 0, "createTimeNs": 0,
        "feed_risk_info": {"declaration_type": 0, "iconUrl": "", "risk_content": ""},
        "feed_source_type": 0, "feed_type": 2, "files": [], "id": "", "images": None,
        "media_lock_count": 0,
        "patternInfo": json.dumps(pattern_info, ensure_ascii=False),
        "poi": {"ad_info": {"adcode": 0, "city": "", "district": "", "province": ""},
                "address": "", "location": {"lat": 0, "lng": 0}, "poi_id": "", "title": ""},
        "poster": {"icon": {"iconUrl": ""}, "id": "", "nick": ""},
        "recommend_channels": [], "tagInfos": [],
        "third_bar": {"button_scheme": "", "content_scheme": "", "id": ""},
        "title": {"contents": ([{"pattern_id": "", "text_content": {"text": title}, "type": 1}] if title else [])},
        "topic_contents": None, "videos": None,
    }
    return {
        "client_content": {},
        "feed": {
            "channelInfo": {"sign": _channel_sign(p)},
            "poster": {"id": ""},
        },
        "jsonFeed": json.dumps(json_feed, ensure_ascii=False),
    }


def _args_get_my_join_guild_info(p: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "bytesCookie": str(p.get("bytes_cookie") or ""),
        "filter": {
            "filter": dict.fromkeys(("uint32CreateTime", "uint32FaceSeq", "uint32GuildName", "uint32GuildNumber", "uint32MemberNum", "uint32Profile"), 1),
            "userFilter": {"uint32Role": 1},
        },
    }


#: (domain, action) → MCP 工具名（与「action 蛇形」不同的才需要列）
TOOL_NAME_OVERRIDES: dict[tuple[str, str], str] = {
    ("feed", "get-notices"): "get_interact_notice",
    ("manage", "guild-member-search"): "guild_member_search",
    ("manage", "get-my-join-guild-info"): "get_my_join_guild_info",
    ("manage", "push-group-dm-msg"): "push_group_normal_dm_msg",
}

#: (domain, action) → 参数整形器（实测捕获；未列入的走 snake→camel 通用规则）
ARG_BUILDERS: dict[tuple[str, str], Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    ("feed", "get-notices"): _args_get_interact_notice,
    ("feed", "get-guild-feeds"): _args_get_guild_feeds,
    ("feed", "get-feed-comments"): _args_get_feed_comments,
    ("feed", "get-feed-detail"): _args_get_feed_detail,
    ("feed", "get-share-url"): _args_get_share_url,
    ("manage", "get-user-info"): _args_get_user_info,
    ("manage", "get-guild-info"): _args_get_guild_info,
    ("manage", "get-guild-channel-list"): _args_get_guild_channel_list,
    ("manage", "get-share-info"): _args_get_share_info,
    ("manage", "guild-member-search"): _args_guild_member_search,
    ("manage", "get-my-join-guild-info"): _args_get_my_join_guild_info,
    ("manage", "search-guild-content"): _args_search_guild_content,
    ("feed", "do-comment"): _args_do_comment,
    ("feed", "do-feed-prefer"): _args_do_feed_prefer,
    ("feed", "do-reply"): _args_do_reply,
    ("feed", "do-like"): _args_do_like,
    ("feed", "publish-feed"): _args_publish_feed,
}

# ── 归一化层：网关原始 sc → CLI schema（插件解析层只认后者）──────────


def _norm_get_my_join_guild_info(sc: Any) -> Any:
    """``msgRspSortGuilds``（base64 字段）→ CLI 的 ``{created_guilds, joined_guilds, ...}``。

    CLI 条目含 share_url（来自组合调用 get_share_url），解析层不消费，置空。
    """
    if not isinstance(sc, dict):
        return sc
    joined: list[dict[str, Any]] = []
    for entry in sc.get("msgRspSortGuilds") or []:
        if not isinstance(entry, dict):
            continue
        info = entry.get("msgGuildInfo") or {}
        user = entry.get("guildUserInfo") or {}
        joined.append({
            "guild_id": str(entry.get("uint64GuildId") or ""),
            "guild_number": _b64(info.get("bytesGuildNumber")),
            "name": _b64(info.get("bytesGuildName")),
            "member_count": info.get("uint32MemberNum"),
            "role": "成员" if user.get("uint32IsMember") == 2 else str(user.get("uint32IsMember") or ""),
            "share_url": "",
        })
    return {"created_guilds": [], "joined_guilds": joined, "managed_guilds": [], "total_count": len(joined)}


def _norm_get_share_info(sc: Any) -> Any:
    """``shareGuildInfo``（驼峰嵌套）→ CLI 的顶级 snake_case 拍平。"""
    if isinstance(sc, dict) and isinstance(sc.get("shareGuildInfo"), dict):
        info = sc["shareGuildInfo"]
        return {
            "guild_id": str(info.get("guildId") or ""),
            "guild_name": str(info.get("guildName") or ""),
            "guild_number": str(info.get("guildNumber") or ""),
        }
    return sc


#: 互动通知类型枚举 → CLI 中文标签。前四组为真机配对实测；其余按 CLI
#: 分类语义推断（文档：点赞/评论/回复/收藏），未收录的枚举原样透传。
_NOTICE_TYPE_LABELS: dict[str, str] = {
    "NOTICE_TYPE_AT_ME": "@我",
    "NOTICE_TYPE_AT_MEW": "@我",
    "NOTICE_TYPE_FEED_AT_ME": "@我",
    "NOTICE_TYPE_PSV_DOAT": "@我",
    "NOTICE_TYPE_PSV_DOCOMMENT": "评论",
    "NOTICE_TYPE_PSV_DOREPLY": "回复",
    "NOTICE_TYPE_PSV_DOLIKE_REPLY": "回复点赞",
    "NOTICE_TYPE_PSV_DOLIKE_COMMENT": "评论点赞",
    "NOTICE_TYPE_PSV_DOLIKE_FEED": "点赞",
    "NOTICE_TYPE_PSV_DOPOLYLIKE_FEED": "点赞",
    "NOTICE_TYPE_PSV_DOPOLYLIKE_REPLY": "回复点赞",
    "NOTICE_TYPE_PSV_DOFAVOR": "收藏",
}


def _rich_text(node: Any) -> str:
    """拼接富文本 ``contents[].textContent.text``（跳过表情/type 7 等非文本段）。"""
    if not isinstance(node, dict):
        return ""
    pieces: list[str] = []
    for part in node.get("contents") or []:
        if not isinstance(part, dict):
            continue
        text = (part.get("textContent") or {}).get("text")
        if text:
            pieces.append(str(text))
    return "".join(pieces)


def _norm_get_interact_notice(sc: Any) -> Any:
    """原始通知条目 → CLI 六键形态（+create_time_raw，比 CLI 多还原一个时间戳）。

    字段来源（真机对照）：feed_id/guild_name ← origineFeed；summary ←
    pattonInfo.plainTxt.txtInfo.content 富文本；type ← 枚举映射；时间 ←
    psvFeed.createTime（互动发生的时刻，CLI 同源）。guild_id 保持空串
    （CLI 同款），由适配器按本次轮询的频道补齐。
    """
    if not isinstance(sc, dict):
        return sc
    notices: list[dict[str, Any]] = []
    for raw in sc.get("notices") or []:
        if not isinstance(raw, dict):
            continue
        feed = raw.get("origineFeed") or {}
        channel = feed.get("channelInfo") or {}
        psv_feed = raw.get("psvFeed") or {}
        plain = (raw.get("pattonInfo") or {}).get("plainTxt") or {}
        summary = _rich_text(plain.get("txtInfo", {}).get("content"))
        epoch = str(psv_feed.get("createTime") or feed.get("createTime") or "").strip()
        create_text = ""
        create_raw = 0
        if epoch.isdigit():
            create_raw = int(epoch)
            create_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(create_raw))
        enum_type = str(raw.get("type") or "")
        notices.append({
            "create_time": create_text,
            "create_time_raw": create_raw,
            "feed_id": str(feed.get("id") or ""),
            "guild_id": "",
            "guild_name": str(channel.get("guildName") or ""),
            "summary": summary,
            "type": _NOTICE_TYPE_LABELS.get(enum_type, enum_type),
        })
    result: dict[str, Any] = {"notices": notices}
    if "isFinish" in sc:
        result["isFinish"] = sc["isFinish"]
    if "attachInfo" in sc:
        result["attachInfo"] = sc["attachInfo"]
    return result


def _norm_feed_entry(raw: Mapping[str, Any]) -> dict[str, Any]:
    """单条帖子（get-guild-feeds 条目 / get-feed-detail 的 feed）→ CLI 蛇形扁平形态。

    prefer_count 藏在 ``totalPrefer.preferCount``；title/正文取富文本 text；
    channel_name ← channelInfo.name（版块名）。
    """
    poster = raw.get("poster") or {}
    channel = raw.get("channelInfo") or {}
    prefer = raw.get("totalPrefer") or {}
    epoch = str(raw.get("createTime") or "").strip()
    create_raw = int(epoch) if epoch.isdigit() else 0
    create_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(create_raw)) if create_raw else ""
    return {
        "author": str(poster.get("nick") or ""),
        "author_id": str(poster.get("id") or ""),
        "channel_name": str(channel.get("name") or ""),
        "comment_count": int(raw.get("commentCount") or 0),
        "content_snippet": _rich_text(raw.get("contents")),
        "create_time": create_text,
        "create_time_raw": create_raw,
        "feed_id": str(raw.get("id") or ""),
        "guild_name": str(channel.get("guildName") or ""),
        "prefer_count": int(prefer.get("preferCount") or 0),
        "title": _rich_text(raw.get("title")),
    }


def _norm_get_guild_feeds(sc: Any) -> Any:
    """帖子列表：驼峰+富文本 → CLI 的蛇形扁平形态（真机逐字段对照）。"""
    if not isinstance(sc, dict):
        return sc
    feeds: list[dict[str, Any]] = []
    for raw in sc.get("feeds") or []:
        if not isinstance(raw, dict):
            continue
        feeds.append(_norm_feed_entry(raw))
    result: dict[str, Any] = {"feeds": feeds}
    if "isFinish" in sc:
        result["isFinish"] = sc["isFinish"]
    if "feedAttachInfo" in sc:
        result["feedAttachInfo"] = sc["feedAttachInfo"]
    return result


def _content_parts(rich: Any) -> tuple[str, list[dict[str, str]], list[Any]]:
    """富文本 → (纯文本, at_users, images)；atContent/imageContent 段各自提取。"""
    text_parts: list[str] = []
    at_users: list[dict[str, str]] = []
    images: list[Any] = []
    for part in (rich or {}).get("contents") or []:
        if not isinstance(part, dict):
            continue
        text = (part.get("textContent") or {}).get("text")
        if text:
            text_parts.append(str(text))
        at = part.get("atContent") or {}
        user = at.get("user") if isinstance(at, dict) else None
        if isinstance(user, dict) and user.get("id"):
            at_users.append({"id": str(user.get("id") or ""), "nick": str(user.get("nick") or "")})
        img = part.get("imageContent")
        if isinstance(img, dict):
            images.append(img)
    return "".join(text_parts), at_users, images


def _norm_comment(raw: Mapping[str, Any], *, index: int, id_key: str) -> dict[str, Any]:
    """vecComment/vecReply 条目 → CLI 的 comment/reply 形态（content 字段是 base64 protobuf，弃用走富文本）。"""
    post = raw.get("postUser") or {}
    text, at_users, images = _content_parts(raw.get("richContents"))
    epoch = str(raw.get("createTime") or "").strip()
    create_raw = int(epoch) if epoch.isdigit() else 0
    entry: dict[str, Any] = {
        "author": str(post.get("nick") or ""),
        "author_id": str(post.get("id") or ""),
        id_key: str(raw.get("id") or ""),
        "content": {"at_users": at_users, "images": images, "sticker": None, "text": text},
        "content_text": text,
        "create_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(create_raw)) if create_raw else "",
        "create_time_raw": create_raw,
        "like_count": int((raw.get("likeInfo") or {}).get("count") or 0),
    }
    if index:
        entry["comment_index"] = index
    target = raw.get("targetReplyId")
    if target:
        entry["target_reply_id"] = str(target)
    target_user = raw.get("targetUser") or {}
    if target_user.get("id"):
        entry["target_user"] = {"id": str(target_user.get("id") or ""), "nick": str(target_user.get("nick") or "")}
    return entry


def _norm_get_feed_comments(sc: Any) -> Any:
    """``vecComment``（含 vecReply 楼中楼）→ CLI 的 ``comments``/``replies_preview`` 形态。"""
    if not isinstance(sc, dict):
        return sc
    comments: list[dict[str, Any]] = []
    for i, raw in enumerate(sc.get("vecComment") or []):
        if not isinstance(raw, dict):
            continue
        entry = _norm_comment(raw, index=i + 1, id_key="comment_id")
        replies = raw.get("vecReply") or []
        entry["replies_preview"] = [_norm_comment(r, index=0, id_key="reply_id") for r in replies if isinstance(r, dict)]
        entry["has_more_replies"] = int(raw.get("replyCount") or 0) > len(entry["replies_preview"])
        comments.append(entry)
    result: dict[str, Any] = {"comments": comments}
    if "isFinish" in sc:
        result["has_more"] = not sc["isFinish"]
    if "attachInfo" in sc:
        result["attach_info"] = sc["attachInfo"]
    return result


def _norm_get_user_info(sc: Any) -> Any:
    """``msgUserInfo``（bytes 字段 base64）→ CLI 的昵称三键；附带还原 tiny_id。"""
    if isinstance(sc, dict) and isinstance(sc.get("msgUserInfo"), dict):
        info = sc["msgUserInfo"]
        nick = _b64(info.get("bytesNickName"))
        member = _b64(info.get("bytesMemberName"))
        return {
            "global_nickname": nick,
            "member_name": member,
            "nickname": nick or member,
            "tiny_id": str(info.get("uint64MemberTinyid") or ""),
        }
    return sc


def _norm_guild_member_search(sc: Any) -> Any:
    """``rptMemberList`` → CLI 的 ``members`` 形态（tinyid/nickname 供 tiny_id 反查）。"""
    if isinstance(sc, dict) and isinstance(sc.get("rptMemberList"), list):
        members: list[dict[str, Any]] = []
        for m in sc["rptMemberList"]:
            if not isinstance(m, dict):
                continue
            joined = str(m.get("joinTime") or "").strip()
            members.append({
                "joinTime": joined,
                "joinTime_human": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(joined))) if joined.isdigit() else "",
                "nickname": str(m.get("nickname") or ""),
                "tinyid": str(m.get("tinyid") or ""),
            })
        return {
            "has_more": False,
            "match_count": int(sc.get("memberNum") or len(members)),
            "members": members,
        }
    return sc


def _norm_get_guild_channel_list(sc: Any) -> Any:
    """``guildInfoList[].channelList[]``（channelName 是 base64）→ CLI 的 ``{channels, count, guild_id}``。

    插件用它在「没配 ``channel.channel_id``」时自动取版块 ID（QQ 客户端里看不到版块 ID）。
    """
    if not isinstance(sc, dict):
        return sc
    channels: list[dict[str, Any]] = []
    guild_id = ""
    for entry in sc.get("guildInfoList") or []:
        if not isinstance(entry, dict):
            continue
        entry_guild = str(entry.get("guildId") or "")
        guild_id = guild_id or entry_guild
        for raw in entry.get("channelList") or []:
            if not isinstance(raw, dict):
                continue
            channels.append({
                "channel_id": str(raw.get("channelId") or ""),
                "channel_name": _b64(raw.get("channelName")),
                "guild_id": entry_guild,
            })
    return {"channels": channels, "count": str(len(channels)), "guild_id": guild_id}


def _norm_get_feed_detail(sc: Any) -> Any:
    """``{"feed": <富形态>}`` → CLI 同构，内层复用帖子条目映射。"""
    if isinstance(sc, dict) and isinstance(sc.get("feed"), dict):
        return {"feed": _norm_feed_entry(sc["feed"])}
    return sc


#: MCP 工具名 → 归一化函数（成功响应的 structuredContent 进，CLI 形态出）
NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    "get_my_join_guild_info": _norm_get_my_join_guild_info,
    "get_share_info": _norm_get_share_info,
    "get_interact_notice": _norm_get_interact_notice,
    "get_guild_feeds": _norm_get_guild_feeds,
    "get_feed_comments": _norm_get_feed_comments,
    "get_feed_detail": _norm_get_feed_detail,
    "get_guild_channel_list": _norm_get_guild_channel_list,
    "get_user_info": _norm_get_user_info,
    "guild_member_search": _norm_guild_member_search,
}


#: 已实测的网关原始形态备忘（供归一化层实现时对照；CLI 输出 = 重排后的目标形态）：
#: - get_my_join_guild_info → sc={bytesCookie, msgRspSortGuilds}；CLI 输出
#:   {created_guilds, joined_guilds, managed_guilds, total_count}，条目含 guild_id/
#:   guild_number/guild_name（guild_resolver.iter_guild_entries 消费）
#: - get-share-info → sc={shareGuildInfo:{guildId,guildName,guildNumber}}；CLI 拍平为顶级 snake_case
#: - get-notices → sc={attachInfo,isFinish,notices[]}，条目 {origineFeed,pattonInfo,psvFeed,status,type}；
#:   CLI 条目 {create_time,feed_id,guild_id,guild_name,summary,type}（补频道名/摘要/时间归一）
#: - get-guild-feeds → sc={feedAttachInfo,feeds[]}，条目 {channelInfo,commentCount,contents,
#:   createTime,feedType,id,poster,share,title富文本,totalPrefer}；CLI 拍平为
#:   {feed_id,author,author_id,title纯文本,content_snippet,create_time,create_time_raw,
#:   comment_count,prefer_count,channel_name,guild_name}


def resolve_mcp_tool(domain: str, action: str) -> str:
    """CLI 命令 → MCP 工具名；未列入的按蛇形还原。"""
    return TOOL_NAME_OVERRIDES.get((domain, action), action.replace("-", "_"))


def build_mcp_args(domain: str, action: str, params: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """CLI 命令+参数 → (MCP 工具名, arguments)。纯函数，便于离线测试。"""
    tool = resolve_mcp_tool(domain, action)
    builder = ARG_BUILDERS.get((domain, action))
    if builder is not None:
        return tool, builder(params)
    return tool, {_snake_to_camel(k): v for k, v in params.items() if v is not None}


def map_gateway_response(resp: Any) -> CliResult:
    """网关 JSON-RPC 响应 → :class:`CliResult`。纯函数，便于离线测试。

    成功：``data = result.structuredContent``（与 CLI 模式的 data 同源同形）；
    失败：``ret_code = _meta.AdditionalFields.retCode``、``error = errMsg``，
    kind 走与 CLI 模式同一套 :func:`classify`（151→auth、8010→invalid_param、
    10014→gone 均已实测一致）。
    """
    if not isinstance(resp, dict) or not isinstance(resp.get("result"), dict):
        preview = json.dumps(resp, ensure_ascii=False)[:200] if isinstance(resp, (dict, list)) else str(resp)[:200]
        return CliResult(kind="error", error=f"网关响应缺少 result 字段：{preview}", raw=resp if isinstance(resp, dict) else {})
    result = resp["result"]
    meta = (result.get("_meta") or {}).get("AdditionalFields") or {}
    ret_code: Any = meta.get("retCode")
    if isinstance(ret_code, str) and ret_code.lstrip("-").isdigit():
        ret_code = int(ret_code)
    sc = result.get("structuredContent")

    if not result.get("isError") and ret_code in (0, None):
        return CliResult(kind="ok", ok=True, data=sc, raw=resp, ret_code=0)

    error_text = str(meta.get("errMsg") or "")
    if not error_text:
        texts = [str(c.get("text", "")) for c in result.get("content", []) if isinstance(c, dict)]
        error_text = "; ".join(t for t in texts if t)[:500]
    if not error_text and isinstance(resp.get("error"), dict):
        error_text = str(resp["error"].get("message") or resp["error"])
    kind = classify(ret_code, error_text)
    return CliResult(
        kind=kind,
        ok=False,
        data=sc,
        raw=resp,
        ret_code=ret_code if isinstance(ret_code, int) else None,
        error=error_text or f"网关返回失败（isError={result.get('isError')}）",
    )


class GatewayClient(TencentChannelCli):
    """``cli.mode = "gateway"`` 时替代子进程 CLI，直连 MCP 网关。

    ``transport`` 仅供离线测试注入（ ``(payload) -> dict`` 的同步函数）；
    运行路径一律走 urllib POST。
    """

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_GATEWAY_ENDPOINT,
        token: str = "",
        timeout: float = 60.0,
        rate_limit_sleep: float = 70.0,
        rate_limit_multiplier: float = 2.0,
        rate_limit_ceiling: float = 1800.0,
        dry_run: bool = False,
        logger: Any = None,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(path="(gateway)", mode="gateway", timeout=timeout, dry_run=dry_run,
                         rate_limit_sleep=rate_limit_sleep,
                         rate_limit_multiplier=rate_limit_multiplier,
                         rate_limit_ceiling=rate_limit_ceiling, logger=logger)
        self.endpoint = endpoint
        self.token = token
        self._transport = transport
        self._next_id = 1
        self.log.info(
            "gateway 模式：免 CLI 直连 MCP 网关（读 6 条 + 写 6 条命令已真机验证；"
            "扫码登录仍需 CLI 一次——用 export_login_token.py 导出令牌后即可脱离 CLI）"
        )

    def describe(self) -> str:
        """可读描述（adapter 加载日志用）。"""
        return f"gateway:{self.endpoint}（免 CLI 直连，token={len(self.token)} 位）"

    # ── HTTP ────────────────────────────────────────────────

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    # ── 调用主路径 ────────────────────────────────────────────

    async def execute(self, inv: Any, *, retry_on_rate_limit: bool = True) -> CliResult:
        """与基类同契约：限流走**进程级冷却**（fail-fast，见 ``TencentChannelCli.execute``），
        本地不再 sleep 后重试。"""
        gated = self._rate_limit_gate(inv)
        if gated is not None:
            return gated
        started = time.monotonic()
        self.log.debug(f"网关调用: {inv.preview()}")
        result = await self._invoke_gateway(inv, started)
        self._note_call_outcome(result)
        return result

    async def _invoke_gateway(self, inv: Any, started: float) -> CliResult:
        if inv.domain == "login":
            return await self._login_command(inv)
        if inv.domain in ("", "cli"):
            return self._top_level_command(inv)
        if not self.token:
            return CliResult(
                kind="auth",
                error="gateway 模式没有可用令牌：配置 cli.login.token_file（扫码一次后用 export_login_token.py 导出），"
                      "或在本机完成过一次 CLI 扫码登录（插件会直接读凭据存储）",
            )

        tool, args = build_mcp_args(inv.domain, inv.action, inv.params or {})
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }
        self._next_id += 1

        if self.dry_run:
            return CliResult(
                kind="ok", ok=True, ret_code=0,
                data={"command": f"{inv.domain} {inv.action}", "tool": tool, "arguments": args, "dry_run": True},
            )

        try:
            resp = await asyncio.to_thread(self._transport or self._post, payload)
        except asyncio.TimeoutError:
            return self._fail(inv, tool, "timeout", f"网关调用超时（>{self.timeout:.0f}s）", started)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            kind = "auth" if exc.code in (401, 403) else "error"
            return self._fail(inv, tool, kind, f"网关 HTTP {exc.code}：{detail or exc.reason}", started)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            kind = classify(None, str(exc))
            return self._fail(inv, tool, kind, f"网关网络失败：{exc}", started)
        except json.JSONDecodeError as exc:
            return self._fail(inv, tool, "error", f"网关响应不是合法 JSON：{exc}", started)

        result = map_gateway_response(resp)
        normalizer = NORMALIZERS.get(tool)
        if result.ok and normalizer is not None:
            result.data = normalizer(result.data)
        result.argv = (f"{inv.domain} {inv.action}", tool)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if not result.ok:
            self.log.warning(
                f"网关返回失败: kind={result.kind} retCode={result.ret_code} tool={tool} error={result.error}"
            )
        return result

    def _fail(self, inv: Any, tool: str, kind: str, error: str, started: float) -> CliResult:
        return CliResult(
            kind=kind,
            error=error,
            argv=(f"{inv.domain} {inv.action}", tool),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # ── login / 顶层命令（本地实现，不触网关）─────────────────

    async def _login_command(self, inv: Any) -> CliResult:
        if inv.action != "status":
            return CliResult(
                kind="error",
                error=f"gateway 模式不执行 `login {inv.action}`：扫码授权在 CLI 里实现。"
                      "先用 CLI 扫码一次，跑 plugins/tencent_channel/export_login_token.py 导出令牌，"
                      "再配置 cli.login.token_file",
            )
        if not self.token:
            return CliResult(
                kind="auth",
                ok=False,
                error="gateway 模式未配置令牌（cli.login.token_file 或本机凭据存储）",
                data={"valid": False, "tokenSource": "gateway"},
            )
        # 探针：最便宜的已验证读命令（get_interact_notice pageNum=1）
        probe = await self._invoke_tool("get_interact_notice", {"pageNum": 1})
        if probe.ok:
            return CliResult(
                kind="ok", ok=True, ret_code=0,
                data={"message": "已登录（gateway 直连，令牌探针通过）。", "tokenSource": "gateway", "valid": True},
                raw=probe.raw, argv=probe.argv, duration_ms=probe.duration_ms,
            )
        return CliResult(
            kind=probe.kind if probe.kind != "ok" else "auth",
            ok=False,
            ret_code=probe.ret_code,
            error=f"gateway 令牌探针失败：{probe.error}",
            data={"valid": False, "tokenSource": "gateway"},
            raw=probe.raw, argv=probe.argv, duration_ms=probe.duration_ms,
        )

    async def _invoke_tool(self, tool: str, args: dict[str, Any]) -> CliResult:
        inv = _StubInvocation(tool, args)
        started = time.monotonic()
        return await self._invoke_gateway(inv, started)

    def _top_level_command(self, inv: Any) -> CliResult:
        if inv.action == "version":
            return CliResult(
                kind="ok", ok=True, ret_code=0,
                data={"version": "gateway-mode/0.1（插件直连 MCP 网关，无 CLI 子进程）"},
            )
        if inv.action == "doctor":
            return CliResult(
                kind="ok", ok=True, ret_code=0,
                data=[{"name": "gateway 模式", "pass": bool(self.token),
                       "detail": "令牌已配置" if self.token else "未配置令牌（cli.login.token_file）",
                       "hint": "gateway 模式直连 MCP 网关，doctor 仅检查令牌在位"}],
            )
        return CliResult(kind="error", error=f"gateway 模式不支持顶层命令：{inv.action}")


class _StubInvocation:
    """给 ``_invoke_tool`` 用的最小 invocation 形状（只暴露 preview/params/domain/action）。

    ``domain="feed"`` 让请求走 tools/call 分支（``""`` 会被当成顶层本地命令）。
    """

    def __init__(self, tool: str, args: dict[str, Any]) -> None:
        self.domain = "feed"
        self.action = tool
        self.params = args

    def preview(self) -> str:
        return f"(probe) {self.tool_name()}"

    def tool_name(self) -> str:
        return str(self.action)
