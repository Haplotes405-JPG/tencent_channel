"""注入文本规则测试（notice_mapping，零框架依赖）。

这些规则都是「踩过坑才加的」，回归时最容易被改坏：

1. 括号里只写**事实**，不出现「这条在跟你说话」这类催促语；
2. **评论/回复的注入文本不带帖子标题/正文**（否则帖名里的点名会污染整串评论的提及判定）；
3. 「在对机器人说话」只认 @ / 回复我的评论 / 通知箱语义；「正文点名了机器人」只按**本条**正文判定。

运行（在插件目录的**上一级**执行，插件内部一律相对导入）：

    python -m unittest discover -s tencent_channel/tests -t . -p "test_*.py"
"""

from __future__ import annotations

import unittest

from ..notice_mapping import (
    match_comment_by_body,
    mentions_self,
    normalize_notice,
    notice_from_comment,
    notice_from_feed,
    notice_text,
    strip_notice_prefix,
)

SELF = "123456789012345602"
NAMES = ["示例Bot", "SampleBot"]
LOUD_TITLE = "示例Bot，看到请回复，一定要回我呀，我求你们"
FEED = "B_example_feed_0001"


class NoticeTextTest(unittest.TestCase):
    """渲染出的文本事实说明。"""

    def test_no_urging_wording(self) -> None:
        notice = normalize_notice({"category": "reply", "content": "回你了"})
        self.assertNotIn("在跟你说话", notice_text(notice))

    def test_rendered_as_person_message_not_system_notice(self) -> None:
        """注入文本必须是「某人说了什么」，不能带平台标签/元数据块。

        真实事故：``【QQ频道·收到评论】…（评论了你的帖子）`` + ``所属帖子ID：…`` + ``时间：…``
        被决策子代理判成「QQ频道系统的通知消息，并非直接对机器人发起的对话」而拒绝回复。
        """
        notice = normalize_notice({"category": "comment", "content": "谢谢你们～"})
        text = notice_text(notice)
        self.assertIn("在你的帖子下留了言", text)
        for banned in ("【QQ频道", "所属帖子ID", "时间：", "（评论了你的帖子）"):
            self.assertNotIn(banned, text)
        self.assertEqual(text.count("\n"), 0, "注入文本应为单行（昵称/时间由框架消息行提供）")

    def test_notice_path_directed_leads(self) -> None:
        cases = {
            "comment": "在你的帖子下留了言",
            "reply": "回复了你的留言",
            "at": "在频道里 @ 了你",
            "dm": "私信你",
            "like": "点了赞",
            "top": "顶了你的帖子",
        }
        for category, expect in cases.items():
            with self.subTest(category=category):
                notice = normalize_notice({"category": category, "content": "内容"})
                self.assertIn(expect, notice_text(notice))

    def test_comment_injection_has_no_feed_title(self) -> None:
        """帖子标题/正文（以及帖子 ID 这类元数据）都不该出现在评论的注入文本里。"""
        for kind, item in (
            ("comment", {"author": "成员B", "author_id": "1", "comment_id": "c_1", "content_text": "大家晚上好"}),
            ("reply", {"author": "成员B", "author_id": "1", "reply_id": "r_1", "content_text": "我也觉得"}),
        ):
            with self.subTest(kind=kind):
                notice = notice_from_comment(
                    item, feed_id=FEED, feed_title=LOUD_TITLE, self_tiny_id=SELF,
                    self_names=NAMES, kind=kind, parent_comment_id="c_0", parent_author_id="1",
                )
                text = notice_text(notice)
                self.assertNotIn(LOUD_TITLE, text)
                self.assertNotIn(FEED, text)
                self.assertIn("（未提及你）", text)

    def test_channel_conversation_is_not_directed(self) -> None:
        notice = notice_from_comment(
            {"author": "成员B", "author_id": "1", "comment_id": "c_1", "content_text": "今晚聊点别的"},
            feed_id=FEED, feed_title=LOUD_TITLE, self_tiny_id=SELF, self_names=NAMES,
        )
        self.assertFalse(notice["directed_at_self"])
        self.assertFalse(notice["mentioned_self"])
        text = notice_text(notice)
        self.assertIn("在频道里留了言", text)
        self.assertIn("（未提及你）", text)

    def test_reply_in_channel_conversation_says_楼中楼(self) -> None:
        notice = notice_from_comment(
            {"author": "成员B", "author_id": "1", "reply_id": "r_9", "content_text": "我也觉得"},
            feed_id=FEED, self_tiny_id=SELF, self_names=NAMES,
            kind="reply", parent_comment_id="c_1", parent_author_id="1",
        )
        self.assertIn("在楼中楼里说了话", notice_text(notice))

    def test_mention_only_from_own_text(self) -> None:
        """本条正文点名 → 提及；只有帖子标题点名 → 不算。"""
        mentioned = notice_from_comment(
            {"author": "某位成员", "author_id": "2", "comment_id": "c_2", "content_text": "示例Bot你倒是说句话呀"},
            feed_id=FEED, feed_title=LOUD_TITLE, self_tiny_id=SELF, self_names=NAMES,
        )
        self.assertTrue(mentioned["mentioned_self"])
        self.assertIn("（提到了你）", notice_text(mentioned))

    def test_at_and_reply_to_me_are_directed(self) -> None:
        at_self = notice_from_comment(
            {"author": "某位成员", "author_id": "2", "comment_id": "c_3", "content_text": "@示例Bot 来",
             "content": {"at_users": [{"id": SELF}]}},
            feed_id=FEED, self_tiny_id=SELF, self_names=NAMES,
        )
        self.assertTrue(at_self["directed_at_self"])
        self.assertIn("在频道里 @ 了你", notice_text(at_self))

        reply_to_me = notice_from_comment(
            {"author": "某位成员", "author_id": "2", "reply_id": "r_2", "content_text": "说得好"},
            feed_id=FEED, self_tiny_id=SELF, self_names=NAMES,
            kind="reply", parent_comment_id="c_me", parent_author_id=SELF,
        )
        self.assertTrue(reply_to_me["directed_at_self"])
        self.assertIn("回复了你的留言", notice_text(reply_to_me))

    def test_feed_notice_keeps_body(self) -> None:
        notice = notice_from_feed(
            {"id": FEED, "title": LOUD_TITLE, "author": "某位成员", "author_id": "2",
             "content_snippet": LOUD_TITLE},
            guild_id="1", self_names=NAMES,
        )
        text = notice_text(notice)
        self.assertIn("在频道里发了新帖", text)
        self.assertIn("（提到了你）", text)
        self.assertIn(LOUD_TITLE, text)  # 新帖本身就是正文，允许出现


class MentionsSelfTest(unittest.TestCase):
    """昵称匹配的边界。"""

    def test_short_names_ignored(self) -> None:
        self.assertFalse(mentions_self("宝", ["宝"]))          # 单字不参与匹配
        self.assertFalse(mentions_self("", NAMES))

    def test_case_insensitive(self) -> None:
        self.assertTrue(mentions_self("SAMPLEBOT 在吗", NAMES))


class SummaryPrefixTest(unittest.TestCase):
    """平台 summary 的动作前缀要剥掉，正文才是「这条评论本身」。"""

    def test_strip_known_prefixes(self) -> None:
        cases = {
            "评论了我的帖子:你好": "你好",
            "回复了我:好的": "好的",
            "回复了我的评论:收到": "收到",
            "赞了我的评论:": "",
            "赞了我的回复:": "",          # 点赞「回复」时平台文案是「赞了我的回复:」
            "赞了我的帖子:": "",
            "收藏了我的帖子:": "",
            "顶了我的帖子:": "",
            "@了我:在吗": "在吗",
            "没有前缀的正文": "没有前缀的正文",
            "": "",
        }
        for raw_text, expect in cases.items():
            with self.subTest(raw=raw_text):
                self.assertEqual(strip_notice_prefix(raw_text), expect)

    def test_normalize_strips_summary_prefix(self) -> None:
        notice = normalize_notice({"type": "评论", "summary": "评论了我的帖子:示例Bot谢谢你们～"})
        self.assertEqual(notice["content"], "示例Bot谢谢你们～")
        self.assertNotIn("评论了我的帖子", notice_text(notice))

    def test_real_comment_text_not_touched(self) -> None:
        """真正的评论正文不带 summary 字段时不应被剥（避免误伤「评论:…」这种正文）。"""
        notice = normalize_notice({"type": "评论", "content": "评论:我觉得挺好"})
        self.assertEqual(notice["content"], "评论:我觉得挺好")


class MatchCommentByBodyTest(unittest.TestCase):
    """通知不带 comment_id 时，按正文回查评论者（绝不能拿帖子作者顶替）。"""

    COMMENTS = [
        {"author": "某位成员", "author_id": "u1", "comment_id": "c1",
         "content_text": "今天的图真好看～"},
        {"author": "成员B", "author_id": "u2", "comment_id": "c2",
         "content_text": "大家晚上好", "replies_preview": [
             {"author": "某位成员", "author_id": "u1", "reply_id": "r1", "content_text": "我也在"}
         ]},
    ]

    def test_exact_match(self) -> None:
        found = match_comment_by_body(self.COMMENTS, "今天的图真好看～")
        self.assertIsNotNone(found)
        self.assertEqual(found["comment_id"], "c1")

    def test_match_inside_replies(self) -> None:
        found = match_comment_by_body(self.COMMENTS, "我也在")
        self.assertIsNotNone(found)
        self.assertEqual(found["reply_id"], "r1")

    def test_ambiguous_returns_none(self) -> None:
        comments = [
            {"author": "甲", "author_id": "a", "comment_id": "c1", "content_text": "同一句话"},
            {"author": "乙", "author_id": "b", "comment_id": "c2", "content_text": "同一句话"},
        ]
        self.assertIsNone(match_comment_by_body(comments, "同一句话"))

    def test_no_match_and_empty_body(self) -> None:
        self.assertIsNone(match_comment_by_body(self.COMMENTS, "完全不相干的内容"))
        self.assertIsNone(match_comment_by_body(self.COMMENTS, ""))
        self.assertIsNone(match_comment_by_body([], "随便"))

    def test_loose_containment(self) -> None:
        found = match_comment_by_body(self.COMMENTS, "今天的图真好看")
        self.assertIsNotNone(found)
        self.assertEqual(found["author_id"], "u1")


if __name__ == "__main__":
    unittest.main()
