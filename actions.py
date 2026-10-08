"""LLM 可调用的写操作（Action）。

规范：Action 偏副作用，``execute`` 返回 ``(bool, str)``。

安全边界：本插件只实现**非破坏性**写操作 —— ``comment_type`` / ``reply_type`` 固定为 1，
永不传 ``--yes``，不实现删帖/删评论/禁言/踢人/退频道等高风险命令。
"""

from __future__ import annotations

from typing import Annotated, ClassVar

from ._compat import BaseAction
from ._component import ChannelCliMixin
from .capabilities import (
    CONFIRM_REQUIRED_KINDS,
    KIND_DANGER,
    KIND_READ,
    KIND_SENSITIVE,
    KIND_WRITE,
    SAFE_GUARDS,
    any_enabled,
    capability_by_name,
    capability_enabled,
    plugin_enabled,
)
from .cli_client import (
    check_content_file,
    check_local_files,
    normalize_params,
    parse_at_users,
    parse_links,
)
from .reply_sender import (
    enrich_comment_meta,
    enrich_feed_meta,
    resolve_reply_target,
    send_comment_reply,
    send_dm_reply,
    to_epoch_seconds,
    truncate,
)

#: 平台契约（tencent-channel-cli 1.0.10 ``feed publish-feed``）
_FEED_SHORT_MAX = 1000
_FEED_LONG_MAX = 10000
_FEED_SHORT_IMAGES = 18
_FEED_LONG_IMAGES = 50
_FEED_SHORT_VIDEOS = 1
_FEED_LONG_VIDEOS = 5


class _ChannelAction(ChannelCliMixin, BaseAction):
    """本插件所有 Action 的公共基类。

    框架在注册 Action 组件时会调用 ``validate_associated_types()``，
    要求 ``associated_types`` 是非空 ``list[str]``，否则整个插件注册中断。
    本插件的消息体是纯文本，故统一声明 ``["text"]``。

    开关：``capability_names`` 声明该动作代表哪些 CLI 能力，
    ``go_activate()`` 会在这些能力**全部关闭**时返回 False，框架随即把该动作
    从本轮的可用组件里摘掉（日志里会看到「go_activate 返回 False」）。
    """

    associated_types: list[str] = ["text"]

    #: 该动作代表的 CLI 能力名（任一开启即保留；留空表示始终保留）
    capability_names: ClassVar[tuple[str, ...]] = ()

    async def go_activate(self) -> bool:
        """按插件总开关 + [capabilities] 开关决定本动作是否可用。"""
        config = self.config()
        if not plugin_enabled(config):
            return False
        if not self.capability_names:
            return True
        return any(capability_enabled(config, name) for name in self.capability_names)


class ChannelPublishFeedAction(_ChannelAction):
    """在腾讯频道发帖（短贴 / 长贴 / Markdown / 图片 / 视频）。"""

    action_name = "channel_publish_feed"
    capability_names = ("feed.publish-feed",)
    action_description = (
        "在腾讯频道发帖。不填 title 是短贴（纯文本 ≤1000 字）；填了 title 就是长贴（≤10000 字，"
        "可用 markdown_content 发 Markdown 富文本，支持标题/粗体/列表/表格/代码块/引用）。"
        "可带图片（短贴≤18、长贴≤50）与视频（短贴 1、长贴 5）。"
        "正文内联语法：@[昵称](tinyid)、[文字](https://url)、#[话题名]()；裸 URL 不可点击。"
        "content_file 只能读实例 data/ 目录下的 txt/md（安全边界）。"
    )

    async def execute(
        self,
        content: Annotated[str, "帖子正文（纯文本，不渲染 Markdown 语法）"] = "",
        title: Annotated[str, "长贴标题；填了就按长贴发（上限 10000 字）"] = "",
        channel_id: Annotated[str, "发到哪个版块：可填版块名（如「闲聊」「全部」）或版块 ID；留空用配置的默认版块"] = "",
        markdown_content: Annotated[
            str, "Markdown 正文（仅长贴可用且必须同时给 title；与 content 互斥）"
        ] = "",
        content_file: Annotated[
            str, "从本地文件读正文（仅限实例 data/ 目录下的 .txt/.md；与 content/markdown_content 互斥）"
        ] = "",
        image_paths: Annotated[list[str], "本地图片路径列表（短贴≤18 张，长贴≤50 张）"] = [],
        video_paths: Annotated[list[str], "本地视频路径列表（短贴≤1，长贴≤5）"] = [],
        topic_names: Annotated[list[str], "话题名列表（追加到正文末尾）"] = [],
        at_users: Annotated[list[str], "要 @ 的人，格式 'tinyid:昵称'（追加到正文末尾）"] = [],
        links: Annotated[list[str], "文字链接，格式 'url|显示文字'（追加到正文末尾）"] = [],
    ) -> tuple[bool, str]:
        """发帖。"""
        guild_id, error = await self.target_guild()
        if error:
            return False, error
        channel, section_error = await self.resolve_section_id(guild_id, channel_id)
        if section_error:
            return False, section_error

        is_long = bool(title) or bool(markdown_content)
        if markdown_content and not title:
            return False, "Markdown 正文只支持长贴：请同时提供 title。"
        if markdown_content and content:
            return False, "content 与 markdown_content 互斥，只填一个。"
        if content_file and (content or markdown_content):
            return False, "content_file 与 content / markdown_content 互斥。"
        if not (content or markdown_content or content_file):
            return False, "至少要提供 content / markdown_content / content_file 之一。"

        body = markdown_content or content
        limit = _FEED_LONG_MAX if is_long else _FEED_SHORT_MAX
        if body and len(body) > limit:
            return False, (
                f"正文 {len(body)} 字，超过{'长贴' if is_long else '短贴'}上限 {limit} 字。"
                "按平台要求不自动拆分：请精简，或加 title 走长贴（上限 10000 字）。"
            )

        images, image_error = check_local_files(image_paths)
        if image_error:
            return False, f"图片不可用：{image_error}"
        max_images = _FEED_LONG_IMAGES if is_long else _FEED_SHORT_IMAGES
        if len(images) > max_images:
            return False, f"{'长贴' if is_long else '短贴'}最多 {max_images} 张图片，收到 {len(images)} 张。"

        videos, video_error = check_local_files(video_paths)
        if video_error:
            return False, f"视频不可用：{video_error}"
        max_videos = _FEED_LONG_VIDEOS if is_long else _FEED_SHORT_VIDEOS
        if len(videos) > max_videos:
            return False, f"{'长贴' if is_long else '短贴'}最多 {max_videos} 个视频，收到 {len(videos)} 个。"

        body_file, file_error = check_content_file(content_file)
        if file_error:
            return False, file_error

        result = await self.cli().publish_feed(
            content,
            guild_id=guild_id,
            channel_id=channel,
            title=title,
            markdown_content=markdown_content,
            content_file=body_file,
            at_users=parse_at_users(at_users),
            topic_names=[str(t).strip() for t in topic_names if str(t).strip()],
            links=parse_links(links),
            image_paths=images,
            video_paths=videos,
        )
        if not result.ok:
            return self.fail(result)
        extras: list[str] = []
        if images:
            extras.append(f"{len(images)} 张图")
        if videos:
            extras.append(f"{len(videos)} 个视频")
        suffix = f"，含 {'、'.join(extras)}" if extras else ""
        return True, f"发帖成功（{'长贴' if is_long else '短贴'}，版块 {channel}{suffix}）。"


class ChannelCommentFeedAction(_ChannelAction):
    """评论一条帖子。"""

    action_name = "channel_comment_feed"
    capability_names = ("feed.do-comment",)
    action_description = "评论腾讯频道里的一条帖子（帖子级评论）。需要帖子 ID；缺少帖子的创建时间时会自动查询补全。"

    async def execute(
        self,
        feed_id: Annotated[str, "帖子 ID（形如 B_xxx），可用 channel_search_feeds 或 channel_notices 获取"],
        content: Annotated[str, "评论正文；只想发一张图片时可以留空"] = "",
        feed_create_time: Annotated[str, "帖子创建时间（秒级时间戳）；留空则自动查询"] = "",
        image_path: Annotated[str, "本地图片路径（可选，最多 1 张，插件会自动上传）"] = "",
    ) -> tuple[bool, str]:
        """评论帖子。"""
        if not feed_id:
            return False, "缺少 feed_id：可用 channel_search_feeds 搜索帖子后取得。"
        if not content and not image_path:
            return False, "content 与 image_path 至少要有一个。"
        guild_id, error = await self.target_guild()
        if error:
            return False, error
        channel_id, section_error = await self.resolve_section_id(guild_id)
        if section_error:
            return False, section_error
        # 接口只认秒级时间戳：LLM 可能传 "2026-10-01 10:19:13" 这类写法，归一后再发
        create_time = to_epoch_seconds(feed_create_time)
        if not create_time:
            meta = await enrich_feed_meta(
                self.cli(), feed_id, guild_id=guild_id, channel_id=channel_id
            )
            create_time = meta.get("feed_create_time") or ""
        text = truncate(content, self.reply_max_length())
        result = await self.cli().do_comment(
            text,
            feed_id=feed_id,
            feed_create_time=create_time,
            guild_id=guild_id,
            channel_id=channel_id,
            image_path=image_path,
        )
        if not result.ok:
            return self.fail(result)
        suffix = "（正文超长已截断）" if text != content else ""
        return True, f"评论已发出{suffix}。"


class ChannelReplyCommentAction(_ChannelAction):
    """楼中楼回复某条评论。"""

    action_name = "channel_reply_comment"
    capability_names = ("feed.do-reply",)
    action_description = (
        "楼中楼回复腾讯频道里的某条评论。插件会自动补齐 do-reply 的必填字段；"
        "字段实在不全时会降级为该帖子的帖内评论，并在结果中说明。"
    )

    async def execute(
        self,
        content: Annotated[str, "回复正文"],
        feed_id: Annotated[str, "帖子 ID（形如 B_xxx）"],
        comment_id: Annotated[
            str,
            "被回复的评论 ID（c_…）。若回复的是某条楼中楼回复，直接传它的回复 ID（r_…）也可以，"
            "插件会自动换算成「一级评论 + target_reply_id」，不会发错位置",
        ],
        guild_id: Annotated[str, "腾讯频道 ID；留空使用插件配置里的默认频道"] = "",
        image_path: Annotated[str, "本地图片路径（可选，最多 1 张，插件会自动上传）"] = "",
    ) -> tuple[bool, str]:
        """回复评论。"""
        if not feed_id or not comment_id:
            return False, "缺少 feed_id 或 comment_id：可用 channel_notices 查看最近通知中的 ID。"
        if not content and not image_path:
            return False, "content 与 image_path 至少要有一个。"
        target_guild, error = await self.target_guild(guild_id)
        if error:
            return False, error
        channel_id, section_error = await self.resolve_section_id(target_guild)
        if section_error:
            return False, section_error
        text = truncate(content, self.reply_max_length())
        outcome = await send_comment_reply(
            self.cli(),
            ctx={
                "feed_id": feed_id,
                "comment_id": comment_id,
                "guild_id": target_guild,
                "channel_id": channel_id,
            },
            text=text,
            guild_id=target_guild,
            channel_id=channel_id,
            self_tiny_id=self.self_tiny_id(),
            reply_to_comment=self.reply_to_comment_enabled(),
            enrich=self.enrich_comment_context_enabled(),
            image_path=image_path,
        )
        if not outcome.ok:
            hint = outcome.result.hint() if outcome.result is not None else ""
            return False, f"{outcome.message}\n处理建议：{hint}"
        return True, outcome.message


class ChannelLikeAction(_ChannelAction):
    """给帖子 / 评论 / 楼中楼回复点赞（或取消自己点的赞）。"""

    action_name = "channel_like"
    capability_names = ("feed.do-like", "feed.do-feed-prefer")
    action_description = (
        "给腾讯频道里的帖子、评论或楼中楼回复点赞（cancel=true 则取消自己点的赞）。"
        "只给 feed_id → 赞帖子；再给 comment_id(c_…) → 赞那条评论；再给 reply_id(r_…) → 赞那条楼中楼回复。"
        "feed_author_id / feed_create_time / comment_author_id / reply_author_id 等必填字段由插件自动查询补齐。"
    )

    async def execute(
        self,
        feed_id: Annotated[str, "帖子 ID（形如 B_xxx）"],
        comment_id: Annotated[str, "评论 ID（c_…）；留空表示给帖子点赞"] = "",
        reply_id: Annotated[str, "楼中楼回复 ID（r_…）；填了它表示给这条回复点赞"] = "",
        cancel: Annotated[bool, "true=取消点赞（只取消自己的赞）；false=点赞"] = False,
        guild_id: Annotated[str, "腾讯频道 ID；留空使用插件配置里的默认频道"] = "",
    ) -> tuple[bool, str]:
        """点赞 / 取消点赞。"""
        if not feed_id:
            return False, "缺少 feed_id：可用 channel_notices 或 channel_search_feeds 获取。"
        target_guild, error = await self.target_guild(guild_id)
        if error:
            return False, error
        channel_id, section_error = await self.resolve_section_id(target_guild)
        if section_error:
            return False, section_error
        client = self.cli()

        # 帖子级点赞：接口只需要 feed_id
        if not comment_id and not reply_id:
            result = await client.do_feed_prefer(
                feed_id,
                action=3 if cancel else 1,
                guild_id=target_guild,
                channel_id=channel_id,
            )
            if not result.ok:
                return self.fail(result)
            return True, ("已取消帖子点赞。" if cancel else "已给帖子点赞。")

        # 评论/回复点赞：先把 do-like 的必填字段补齐
        meta = await enrich_feed_meta(client, feed_id, guild_id=target_guild, channel_id=channel_id)
        feed_author_id = str(meta.get("feed_author_id") or "")
        feed_create_time = str(meta.get("feed_create_time") or "")

        parent_comment_id = comment_id
        reply_author_id = ""
        if reply_id:
            ctx = await resolve_reply_target(
                client, feed_id, reply_id, guild_id=target_guild, channel_id=channel_id
            )
            if not ctx:
                return False, (
                    f"无法定位回复 {reply_id} 所属的一级评论（评论列表里没找到），"
                    "已取消操作，避免点错位置。"
                )
            parent_comment_id = str(ctx.get("comment_id") or comment_id)
            reply_author_id = str(ctx.get("target_user_id") or "")

        comment_author_id = ""
        if parent_comment_id:
            cmeta = await enrich_comment_meta(
                client, feed_id, parent_comment_id, guild_id=target_guild, channel_id=channel_id
            )
            comment_author_id = str(cmeta.get("comment_author_id") or "")

        result = await client.do_like(
            feed_id=feed_id,
            comment_id=parent_comment_id,
            feed_author_id=feed_author_id,
            feed_create_time=feed_create_time,
            comment_author_id=comment_author_id,
            reply_id=reply_id,
            reply_author_id=reply_author_id,
            cancel=cancel,
            guild_id=target_guild,
            channel_id=channel_id,
        )
        if not result.ok:
            return self.fail(result)
        what = "楼中楼回复" if reply_id else "评论"
        return True, (f"已取消{what}点赞。" if cancel else f"已给{what}点赞。")


class ChannelSendDmAction(_ChannelAction):
    """给用户发私信。"""

    action_name = "channel_send_dm"
    capability_names = ("manage.push-group-dm-msg",)
    action_description = (
        "给腾讯频道用户发送私信。需要对方的 tiny_id（先用 channel_members 按昵称查询）。"
        "注意：对方回复之前只能发 1 条（超发会返回 retCode 100707）。"
    )

    async def execute(
        self,
        text: Annotated[str, "私信正文"],
        peer_tiny_id: Annotated[str, "对方 tiny_id（内部用户 ID，不是 QQ 号）"],
        source_guild_id: Annotated[str, "来源频道 ID；留空使用插件配置的频道"] = "",
    ) -> tuple[bool, str]:
        """发送私信。"""
        if not peer_tiny_id:
            return False, "缺少 peer_tiny_id：先用 channel_members 按昵称查询 tiny_id。"
        source, error = await self.target_guild(source_guild_id or self.dm_source_guild_id())
        if error:
            return False, error
        outcome = await send_dm_reply(
            self.cli(),
            ctx={},
            text=truncate(text, self.reply_max_length()),
            source_guild_id=source,
            peer_tiny_id=peer_tiny_id,
        )
        if not outcome.ok:
            hint = outcome.result.hint() if outcome.result is not None else ""
            return False, f"{outcome.message}\n处理建议：{hint}"
        return True, outcome.message


class ChannelWriteAction(_ChannelAction):
    """通用写入动作：执行能力清单里的任意一条写类命令（含敏感/破坏性，按开关放行）。"""

    action_name = "channel_write"
    action_description = "执行腾讯频道的写类命令（可选命令见插件配置 [prompts] write_action）。"

    async def go_activate(self) -> bool:
        """只要插件启用且还有任一写能力开启，本动作就保留。"""
        config = self.config()
        if not plugin_enabled(config):
            return False
        return any_enabled(config, (KIND_WRITE, KIND_SENSITIVE, KIND_DANGER))

    async def execute(
        self,
        command: Annotated[str, "写命令名，形如 'feed.set-feed-essence'；可选值见动作描述里的清单"],
        params: Annotated[dict, "该命令的参数（JSON 对象，键名见动作描述；带 * 的是必填）"] = {},
        confirm: Annotated[bool, "破坏性命令必须显式传 true 才会执行"] = False,
    ) -> tuple[bool, str]:
        """执行一条写命令。"""
        config = self.config()
        if not plugin_enabled(config):
            return False, "腾讯频道插件已在 [plugin] enabled=false 里整体停用。"
        cap = capability_by_name(command)
        if cap is None:
            return False, f"未知命令 {command!r}；可用写命令见动作描述里的清单。"
        if cap.kind == KIND_READ:
            return False, f"{cap.name} 是只读命令，请用 channel_read。"
        if not capability_enabled(config, cap.name):
            return False, (
                f"命令 {cap.name} 已被配置关闭：请在 config.toml 的 [capabilities] 里把它加入 "
                f"enabled（或打开 {cap.kind}_default）。当前关闭的命令包括所有敏感与破坏性操作。"
            )
        if cap.kind in CONFIRM_REQUIRED_KINDS and not confirm:
            return False, (
                f"{cap.name}（{cap.summary}）属于破坏性操作：确认要执行请把 confirm 设为 true。"
            )

        payload = normalize_params(params or {})
        notes: list[str] = []
        for key, allowed in SAFE_GUARDS.get(cap.name, {}).items():
            if key in payload and payload[key] not in allowed:
                notes.append(f"{key} 由 {payload[key]} 收敛为 {allowed[0]}")
                payload[key] = allowed[0]

        if cap.domain != "cli":
            if "guild_id" in cap.params and not payload.get("guild_id"):
                target, error = await self.target_guild("", allow_empty=True)
                if error:
                    return False, error
                if target:
                    payload["guild_id"] = target
            if "channel_id" in cap.params and not payload.get("channel_id"):
                # 版块：允许配置里写名字，也允许这里填名字；解析不出来就交给接口报错（allow_empty）
                guild_for_section = str(payload.get("guild_id") or self.default_guild_id())
                default_channel, _ = await self.resolve_section_id(
                    guild_for_section, allow_empty=True
                )
                if default_channel:
                    payload["channel_id"] = default_channel

        missing = [name for name in cap.required if not payload.get(name)]
        if missing:
            return False, f"{cap.name} 缺少必填参数：{'、'.join(missing)}"

        allow_yes = False
        caps_section = getattr(config, "capabilities", None)
        if cap.kind in CONFIRM_REQUIRED_KINDS and confirm:
            allow_yes = bool(getattr(caps_section, "allow_yes_for_danger", False))

        result = await self.cli().run_command(
            cap.domain, cap.action, payload, yes=allow_yes
        )
        if not result.ok:
            extra = f"（{'；'.join(notes)}）" if notes else ""
            return self.fail(result) if not extra else (
                False,
                f"{cap.name} 执行失败：{result.error}{extra}\n处理建议：{result.hint()}",
            )
        message = f"{cap.name} 执行成功（{cap.summary}）"
        if notes:
            message += "；" + "；".join(notes)
        if allow_yes:
            message += "；已附加 --yes"
        return True, message


#: 供 plugin.py 注册
ACTIONS: list[type] = [
    ChannelPublishFeedAction,
    ChannelCommentFeedAction,
    ChannelReplyCommentAction,
    ChannelLikeAction,
    ChannelSendDmAction,
    ChannelWriteAction,
]
