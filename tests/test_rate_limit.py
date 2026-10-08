"""进程级限流治理测试（``RateLimitGovernor`` + CLI/网关两条 transport 的 ``execute`` 行为）。

覆盖：指数退避曲线、冷却期 fail-fast（零网络调用）、**跨实例/跨 transport 共享冷却**、
成功清零、dry_run 不受冷却拦截。

背景：旧版两条 transport 各自「sleep 70s 原样重试一次」，互不感知——三条轮询路径
叠加出站与工具调用，实测形成重试风暴（连续 4 小时全是 153）。新版所有调用共享
进程级冷却时钟：任意一次真实 153 触发指数退避，冷却期内调用直接快速失败。

运行（在插件目录的**上一级**执行，插件内部一律相对导入）：

    python -m unittest discover -s tencent_channel/tests -t . -p "test_*.py"
"""

from __future__ import annotations

import asyncio
import time
import unittest

from ..cli_client import (
    RATE_LIMIT_GOVERNOR,
    CliInvocation,
    CliResult,
    RateLimitGovernor,
    TencentChannelCli,
)
from ..gateway_client import GatewayClient


def _rate_result(argv: tuple[str, ...] = ("feed", "get-notices")) -> CliResult:
    return CliResult(
        kind="rate_limit",
        ok=False,
        ret_code=153,
        error="业务错误 (retCode=153): 接口调用已超过申请的频率上限",
        argv=argv,
    )


def _ok_result(data: dict | None = None) -> CliResult:
    return CliResult(kind="ok", ok=True, ret_code=0, data=data or {"notices": []})


def _gateway_rate_response() -> dict:
    """真机捕获的网关 153 形态（retCode 在 _meta.AdditionalFields 里）。"""
    return {
        "result": {
            "isError": True,
            "_meta": {"AdditionalFields": {"retCode": 153, "errMsg": "接口调用已超过申请的频率上限"}},
        }
    }


class GovernorBackoffTest(unittest.TestCase):
    """退避曲线：70 → 140 → 280 → 560 → 1120 → 封顶 1800。"""

    def test_progression_and_ceiling(self) -> None:
        gov = RateLimitGovernor()
        now = 1000.0
        waits = []
        for _ in range(6):
            wait = gov.on_rate_limited(base=70.0, multiplier=2.0, ceiling=1800.0, now=now)
            waits.append(wait)
            now += wait
        self.assertEqual(waits, [70.0, 140.0, 280.0, 560.0, 1120.0, 1800.0])
        self.assertEqual(gov.consecutive, 6)

    def test_remaining_window(self) -> None:
        gov = RateLimitGovernor()
        gov.on_rate_limited(base=70.0, now=1000.0)
        self.assertAlmostEqual(gov.remaining(now=1030.0), 40.0)
        self.assertEqual(gov.remaining(now=2000.0), 0.0, "冷却自然过期后 remaining 归零")

    def test_success_resets(self) -> None:
        gov = RateLimitGovernor()
        gov.on_rate_limited(base=70.0, now=1000.0)
        gov.on_rate_limited(base=70.0, now=1100.0)
        gov.on_success()
        self.assertEqual((gov.consecutive, gov.remaining(now=1101.0)), (0, 0.0))
        # 清零后重新从首档起算
        self.assertEqual(gov.on_rate_limited(base=70.0, now=1200.0), 70.0)

    def test_custom_base_and_ceiling(self) -> None:
        gov = RateLimitGovernor()
        self.assertEqual(gov.on_rate_limited(base=10.0, multiplier=3.0, ceiling=25.0, now=0.0), 10.0)
        self.assertEqual(gov.on_rate_limited(base=10.0, multiplier=3.0, ceiling=25.0, now=10.0), 25.0)


class CliTransportGateTest(unittest.TestCase):
    """CLI 模式：冷却期 fail-fast；真实 153 只打一次网、立即返回。"""

    def setUp(self) -> None:
        RATE_LIMIT_GOVERNOR.reset()

    def _client(self, spawn_results: list[CliResult]) -> tuple[TencentChannelCli, dict]:
        """构造打桩客户端：mode=exe 避免触发本机 CLI 自动发现。"""
        cli = TencentChannelCli(path="(fake)", mode="exe", rate_limit_sleep=70.0)
        calls = {"n": 0}

        async def fake_spawn(argv, stdin_bytes, started):  # noqa: ANN001
            calls["n"] += 1
            return spawn_results[min(calls["n"], len(spawn_results)) - 1]

        cli._spawn = fake_spawn  # type: ignore[method-assign]
        return cli, calls

    def test_gate_blocks_without_network_during_cooldown(self) -> None:
        RATE_LIMIT_GOVERNOR.on_rate_limited(base=70.0, now=time.monotonic())
        cli, calls = self._client([_ok_result()])
        inv = cli.new_invocation("feed", "get-notices", {"guild_id": "1"})
        result = asyncio.run(cli.execute(inv))
        self.assertEqual(result.kind, "rate_limit")
        self.assertIn("冷却", result.error)
        self.assertEqual(calls["n"], 0, "冷却期内不得发起任何子进程/网络调用")

    def test_fresh_153_returns_once_and_starts_cooldown(self) -> None:
        cli, calls = self._client([_rate_result(), _ok_result()])
        inv = cli.new_invocation("feed", "get-notices", {"guild_id": "1"})
        result = asyncio.run(cli.execute(inv))
        self.assertEqual(result.kind, "rate_limit")
        self.assertEqual(calls["n"], 1, "旧版会 sleep 70s 后重试一次（共 2 次调用），新版只打一次网")
        self.assertGreater(RATE_LIMIT_GOVERNOR.remaining(), 0.0, "真实 153 应启动进程级冷却")

    def test_cooldown_shared_across_instances(self) -> None:
        cli_a, _ = self._client([_rate_result()])
        asyncio.run(cli_a.execute(cli_a.new_invocation("feed", "get-notices", {})))
        # Tool/Action 每次新建客户端实例（cli_service.client() 非单例），必须同样被拦
        cli_b, calls_b = self._client([_ok_result()])
        result = asyncio.run(cli_b.execute(cli_b.new_invocation("manage", "get-guild-info", {})))
        self.assertEqual(result.kind, "rate_limit")
        self.assertEqual(calls_b["n"], 0, "新建实例不能绕过进程级冷却")

    def test_success_after_expired_cooldown_resets_counter(self) -> None:
        # 用「过去时刻」触发限流：冷却已自然过期（remaining=0），但 consecutive 仍为 1
        RATE_LIMIT_GOVERNOR.on_rate_limited(base=70.0, now=time.monotonic() - 1000.0)
        self.assertEqual(RATE_LIMIT_GOVERNOR.remaining(), 0.0)
        cli, _ = self._client([_ok_result()])
        result = asyncio.run(cli.execute(cli.new_invocation("feed", "get-notices", {})))
        self.assertTrue(result.ok, "冷却过期后下一次真实调用应正常出网")
        self.assertEqual(RATE_LIMIT_GOVERNOR.consecutive, 0, "成功调用应通过客户端路径清零连续计数")

    def test_dry_run_bypasses_gate(self) -> None:
        RATE_LIMIT_GOVERNOR.on_rate_limited(base=70.0, now=time.monotonic())
        cli = TencentChannelCli(path="(fake)", mode="exe", dry_run=True)
        calls = {"n": 0}

        async def fake_spawn(argv, stdin_bytes, started):  # noqa: ANN001
            calls["n"] += 1
            return _ok_result({"dry_run": True})

        cli._spawn = fake_spawn  # type: ignore[method-assign]
        result = asyncio.run(cli.execute(cli.new_invocation("feed", "get-notices", {})))
        self.assertTrue(result.ok, "dry_run 本来就不出网，排障时不应被冷却拦截")
        self.assertEqual(calls["n"], 1)


class GatewayTransportGateTest(unittest.TestCase):
    """网关模式：与 CLI 模式共享同一份进程级冷却。"""

    def setUp(self) -> None:
        RATE_LIMIT_GOVERNOR.reset()

    def test_gateway_153_no_retry_and_shared_cooldown(self) -> None:
        hits = {"n": 0}

        def transport(payload: dict) -> dict:
            hits["n"] += 1
            return _gateway_rate_response()

        gw = GatewayClient(token="t", transport=transport)
        result = asyncio.run(gw.execute(gw.new_invocation("feed", "get-guild-feeds", {"guild_id": "1"})))
        self.assertEqual(result.kind, "rate_limit")
        self.assertEqual(hits["n"], 1, "网关模式同样只打一次网，不再 sleep 后重试")
        self.assertGreater(RATE_LIMIT_GOVERNOR.remaining(), 0.0)

        # CLI 模式的另一个实例被同一份冷却拦住（跨 transport 共享）
        cli = TencentChannelCli(path="(fake)", mode="exe")
        calls = {"n": 0}

        async def fake_spawn(argv, stdin_bytes, started):  # noqa: ANN001
            calls["n"] += 1
            return _ok_result()

        cli._spawn = fake_spawn  # type: ignore[method-assign]
        gated = asyncio.run(cli.execute(cli.new_invocation("feed", "get-notices", {})))
        self.assertEqual(gated.kind, "rate_limit")
        self.assertEqual(calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
