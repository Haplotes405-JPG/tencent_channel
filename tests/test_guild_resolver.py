"""频道号 / 版块解析测试（``guild_resolver``，零框架依赖）。

覆盖：频道号与真实 ID 的拆分、CLI 与网关两种返回形态的解析、按名字解析版块（含失败时的候选提示）。

运行（在插件目录的**上一级**执行，插件内部一律相对导入）：

    python -m unittest discover -s tencent_channel/tests -t . -p "test_*.py"
"""

from __future__ import annotations

import asyncio
import base64
import unittest
from types import SimpleNamespace

from ..guild_resolver import (
    iter_channel_entries,
    looks_like_channel_id,
    looks_like_guild_id,
    resolve_section,
    split_guild_refs,
)

GUILD = "12345678901234567"


class FakeCli:
    """最小 CLI 替身：只实现 ``get_channel_list``。"""

    def __init__(self, payload, *, ok=True, error=""):
        self._payload = payload
        self._ok = ok
        self._error = error
        self.calls: list[str] = []

    async def get_channel_list(self, guild_id: str):
        self.calls.append(guild_id)
        return SimpleNamespace(ok=self._ok, data=self._payload, raw=self._payload, error=self._error)


CLI_PAYLOAD = {"channels": [
    {"channel_id": "12345678902", "channel_name": "闲聊", "guild_id": GUILD},
    {"channel_id": "12345678903", "channel_name": "公告", "guild_id": GUILD},
    {"channel_id": "12345678901", "channel_name": "全部", "guild_id": GUILD},
]}

GATEWAY_PAYLOAD = {"guildInfoList": [{"guildId": GUILD, "channelList": [
    {"channelId": "12345678901", "bytesChannelName": base64.b64encode("全部".encode()).decode()},
    {"channelId": "12345678902", "bytesChannelName": base64.b64encode("闲聊".encode()).decode()},
]}]}


class RefParsingTest(unittest.TestCase):
    def test_split_refs(self) -> None:
        self.assertEqual(split_guild_refs("pd12345678"), ["pd12345678"])
        self.assertEqual(split_guild_refs("pd1, pd2"), ["pd1", "pd2"])
        self.assertEqual(split_guild_refs("pd1、pd2"), ["pd1", "pd2"])
        self.assertEqual(split_guild_refs(["pd1", "  ", "pd2"]), ["pd1", "pd2"])
        self.assertEqual(split_guild_refs(None), [])

    def test_id_detection(self) -> None:
        self.assertTrue(looks_like_guild_id("12345678901234567"))
        self.assertFalse(looks_like_guild_id("pd12345678"))
        self.assertTrue(looks_like_channel_id("12345678902"))
        self.assertFalse(looks_like_channel_id("闲聊"))
        self.assertFalse(looks_like_channel_id(""))


class ChannelEntryTest(unittest.TestCase):
    def test_cli_shape(self) -> None:
        entries = iter_channel_entries(CLI_PAYLOAD)
        self.assertEqual([e["channel_name"] for e in entries], ["闲聊", "公告", "全部"])
        self.assertEqual(entries[0]["channel_id"], "12345678902")

    def test_gateway_shape_decodes_base64_names(self) -> None:
        entries = iter_channel_entries(GATEWAY_PAYLOAD)
        self.assertEqual([e["channel_name"] for e in entries], ["全部", "闲聊"])

    def test_empty_payloads(self) -> None:
        for payload in ({}, {"channels": []}, None, []):
            self.assertEqual(iter_channel_entries(payload), [])


class ResolveSectionTest(unittest.TestCase):
    def _resolve(self, payload, ref, **kwargs):
        cli = FakeCli(payload, **kwargs)
        return asyncio.run(resolve_section(cli, guild_id=GUILD, ref=ref)), cli

    def test_numeric_id_passthrough_without_query(self) -> None:
        result, cli = self._resolve(CLI_PAYLOAD, "12345678902")
        self.assertTrue(result.ok)
        self.assertEqual(result.channel_id, "12345678902")
        self.assertEqual(cli.calls, [], "纯数字应直接使用，不查接口")

    def test_exact_name(self) -> None:
        result, _ = self._resolve(CLI_PAYLOAD, "闲聊")
        self.assertTrue(result.ok)
        self.assertEqual((result.channel_id, result.channel_name), ("12345678902", "闲聊"))

    def test_case_insensitive_and_contains(self) -> None:
        payload = {"channels": [{"channel_id": "1", "channel_name": "All"}]}
        self.assertEqual(self._resolve(payload, "all")[0].channel_id, "1")
        self.assertEqual(self._resolve(payload, "al")[0].channel_id, "1")

    def test_missing_name_lists_candidates(self) -> None:
        result, _ = self._resolve(CLI_PAYLOAD, "不存在的版块")
        self.assertFalse(result.ok)
        self.assertIn("闲聊", result.message)
        self.assertIn("全部", result.candidates)

    def test_empty_ref_and_no_guild(self) -> None:
        result, cli = self._resolve(CLI_PAYLOAD, "")
        self.assertFalse(result.ok)
        self.assertEqual(cli.calls, [])
        result2 = asyncio.run(resolve_section(FakeCli(CLI_PAYLOAD), guild_id="", ref="闲聊"))
        self.assertFalse(result2.ok)

    def test_api_failure_is_reported(self) -> None:
        result, _ = self._resolve(CLI_PAYLOAD, "闲聊", ok=False, error="boom")
        self.assertFalse(result.ok)
        self.assertIn("boom", result.message)

    def test_empty_channel_list(self) -> None:
        result, _ = self._resolve({"channels": []}, "闲聊")
        self.assertFalse(result.ok)
        self.assertIn("没有返回任何版块", result.message)


if __name__ == "__main__":
    unittest.main()
