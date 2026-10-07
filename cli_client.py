"""腾讯频道 CLI（``tencent-channel-cli``）调用客户端。

设计要点
--------
1. **零框架依赖**：只使用标准库，因此本模块可以在 MoFox 打包环境之外单独导入与单测
   （打包环境里 ``src.*`` / ``mofox_wire`` 只在 exe 内部可见，插件逻辑无法在外部 import）。
2. **参数优先走 stdin JSON**：CLI 支持 stdin JSON 传参（snake_case 键名 = flag 名把 ``-`` 换成 ``_``），
   命令行只保留 ``domain action`` 与少量全局 flag。这样可以彻底避开 Windows
   ``cmd.exe`` 的引号/特殊字符/长度问题（评论、发帖正文往往是中文长文本）。
3. **Windows 调用方式**：裸 ``tencent-channel-cli`` 默认走 ``.ps1``，执行策略受限时会无交互永久卡住，
   官方要求改用 ``.cmd``。但 ``.cmd`` 必须经 ``cmd.exe`` 解释，仍会有引号问题，
   所以支持显式 ``mode``：``node`` / ``cmd`` / ``python`` / ``exe`` / ``auto``。
   P0 探测后把最稳的方式写进配置即可。
4. **不抛业务异常**：网络失败、未登录、限流等都以 :class:`CliResult` 返回，
   调用方按 ``kind`` 分支处理，避免每个调用点都写 try/except。
5. **凭证零泄漏**：日志只记录 argv、retCode 与错误消息，不打印 token / 二维码 / 原始大对象。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("tencent_channel.cli_client")

#: 限流（文档硬规则：sleep 70s 后原样重试一次）
RATE_LIMIT_RET_CODES: frozenset[int] = frozenset({153})
RATE_LIMIT_MARKERS: tuple[str, ...] = ("接口调用已超过申请的频率上限", "频率上限", "rate limit")
#: 鉴权失败（151 = 登录态验证失败：生产日志实测，CLI 不带 retCode=未登录 那种统一文案）
AUTH_RET_CODES: frozenset[int] = frozenset({8011, 100007, 151})
AUTH_MARKERS: tuple[str, ...] = (
    "未登录",
    "鉴权失败",
    "登录已过期",
    "登录态验证失败",
    "登录态失效",
    "请重新登录",
)
#: 需要先加入频道
JOIN_REQUIRED_RET_CODES: frozenset[int] = frozenset({20047, 130000, 20006})
#: 对方未回复前私信只能发一条
DM_LIMIT_RET_CODES: frozenset[int] = frozenset({100707})
#: 参数无效（10000 = 参数校验失败；8010 = 字段格式不正确，生产实测卡在 createTime；
#: 8004 = 字段解析失败，实测出现在 gateway 模式把空 channel_id 拼进 channelSign 时）
INVALID_PARAM_RET_CODES: frozenset[int] = frozenset({10000, 8010, 8004})
#: 内容已删除 / 不存在（10014 = 数据已被删除：对已删帖子翻评论时实测）
GONE_RET_CODES: frozenset[int] = frozenset({10014})
GONE_MARKERS: tuple[str, ...] = ("已被删除", "数据不存在", "not exist")
#: 网络层失败（CLI 出口连不上或被本机软件断开，生产实测：wsarecv / connection aborted）
NETWORK_MARKERS: tuple[str, ...] = (
    "网络请求失败",
    "wsarecv",
    "connection aborted",
    "connection refused",
    "connection reset",
    "dial tcp",
    "no such host",
    "network is unreachable",
    "context deadline exceeded",
)

_RET_CODE_RE = re.compile(r"retCode\s*[=:]\s*(\d+)", re.IGNORECASE)


def kebab(name: str) -> str:
    """把 snake_case 参数名转成 CLI 的 ``--kebab-case``。"""
    return name.strip().replace("_", "-")


def build_flag_tokens(params: Mapping[str, Any]) -> list[str]:
    """把参数字典转成 CLI flag token 列表（纯函数，便于单测）。

    - ``None`` 跳过
    - ``True`` → 只给 ``--flag``；``False`` 跳过
    - 列表/元组 → 重复该 flag
    - 其它 → ``--flag value``
    """
    tokens: list[str] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        flag = f"--{kebab(key)}"
        if isinstance(value, bool):
            if value:
                tokens.append(flag)
            continue
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if item is None:
                    continue
                tokens.extend([flag, str(item)])
            continue
        tokens.extend([flag, str(value)])
    return tokens


@dataclass
class CliInvocation:
    """一次 CLI 调用的描述（不执行）。"""

    domain: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, Any] = field(default_factory=dict)
    #: True 时业务参数走 stdin JSON，False 时走命令行 flag
    use_stdin: bool = True
    json_output: bool = True
    dry_run: bool = False
    #: 允许 CLI 以纯文本（非 JSON）输出，例如 ``version``
    allow_plain: bool = False

    @property
    def action_path(self) -> str:
        return f"{self.domain} {self.action}" if self.domain else self.action

    def argv_tokens(self) -> list[str]:
        tokens: list[str] = []
        if self.domain:
            tokens.append(self.domain)
        if self.action:
            tokens.append(self.action)
        tokens.extend(build_flag_tokens(self.flags))
        if self.json_output:
            tokens.append("--json")
        if self.dry_run:
            tokens.append("--dry-run")
        return tokens

    def stdin_bytes(self) -> bytes | None:
        if not self.use_stdin or not self.params:
            return None
        payload = {k: v for k, v in self.params.items() if v is not None}
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def preview(self) -> str:
        """人类可读的命令预览（日志用；正文以占位符代替）。"""
        parts = ["tencent-channel-cli", *self.argv_tokens()]
        if self.stdin_bytes() is not None:
            keys = ", ".join(sorted(k for k, v in self.params.items() if v is not None))
            parts.append(f"<stdin:{keys}>")
        return " ".join(parts)


@dataclass
class CliResult:
    """CLI 调用结果。

    Attributes:
        kind: ``ok`` / ``business`` / ``auth`` / ``rate_limit`` / ``join_required`` /
        ``dm_limit`` / ``invalid_param`` / ``gone`` / ``network`` / ``not_found`` /
        ``timeout`` / ``error``
        ok: 业务是否成功（``success=true`` 且 retCode 为 0/缺省）
        data: 成功时的 ``data`` 字段
        raw: CLI 输出的完整 JSON 对象
        error: 错误文本
        ret_code: 业务 retCode
        argv: 实际执行的 argv（便于排障）
        duration_ms: 耗时
        stderr: 标准错误（截断）
    """

    kind: str = "ok"
    ok: bool = False
    data: Any = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    ret_code: int | None = None
    argv: tuple[str, ...] = ()
    duration_ms: int = 0
    stderr: str = ""
    stdout: str = ""
    returncode: int = 0

    def hint(self) -> str:
        """给运维/LLM 看的中文处理建议（只给分类器实测覆盖过的形态，business 透原始错误）。"""
        if self.kind == "auth":
            return (
                "鉴权失败/登录态失效：已配置 cli.login.token 或 cli.login.token_file 时重启会自动用令牌恢复"
                "（令牌过期就重跑 plugins/tencent_channel/export_login_token.py 导出）；"
                "否则请在终端执行 `tencent-channel-cli.cmd login --json` 扫码重新登录（插件不会代跑登录）。"
            )
        if self.kind == "rate_limit":
            return "接口触发频率限制，请稍后再试（已按规则 sleep 70s 重试过一次）。"
        if self.kind == "join_required":
            return "频道需先加入才能互动/浏览：用 `manage search-and-join --keyword \"频道名\" --json` 加入后重试。"
        if self.kind == "dm_limit":
            return "对方回复前私信只能发送 1 条（retCode 100707）。"
        if self.kind == "invalid_param":
            if self.ret_code == 8010:
                return (
                    "字段格式不正确（retCode 8010，实测卡在 createTime）：确认 reply.enrich_comment_context=true，"
                    "时间字段须为秒级时间戳（毫秒需除以 1000）。"
                )
            if self.ret_code == 8004:
                return (
                    "字段解析失败（retCode 8004，实测出现在 ID 字段传了空串时，例如 channelSign.channel_id=\"\"）："
                    "gateway 模式下空的频道/版块 ID 必须**整个省略**而不是填空串；"
                    "若报错来自 channel_id，请填 channel.channel_id（版块 ID，`manage get-guild-channel-list` 可查）。"
                )
            return "参数无效（retCode 10000）：写操作通常需要同时带上 guild_id 与 channel_id。"
        if self.kind == "gone":
            return "内容已被删除或不存在（retCode 10014）：跳过该条即可；评论轮询会自动略过这类帖子，不影响主流程。"
        if self.kind == "network":
            return "网络层失败（CLI 出口连不上/被本机软件断开）：检查网络、代理与防火墙对 graph.qq.com 的拦截，稍后重试。"
        if self.kind == "not_found":
            return "未找到 tencent-channel-cli：请先 `npm install -g tencent-channel-cli` 并确认 cli.path 配置。"
        if self.kind == "timeout":
            return "CLI 调用超时：检查网络或调大 cli.timeout。"
        return self.error


class TencentChannelCli:
    """``tencent-channel-cli`` 的异步调用封装。"""

    def __init__(
        self,
        path: str = "tencent-channel-cli.cmd",
        *,
        mode: str = "auto",
        timeout: float = 60.0,
        dry_run: bool = False,
        node_path: str = "",
        python_path: str = "",
        rate_limit_sleep: float = 70.0,
        extra_env: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.path = (path or "").strip() or "tencent-channel-cli.cmd"
        self.mode = (mode or "auto").strip().lower()
        self.timeout = float(timeout)
        self.dry_run = bool(dry_run)
        self.node_path = node_path.strip()
        self.python_path = python_path.strip()
        self.rate_limit_sleep = float(rate_limit_sleep)
        self.extra_env = dict(extra_env or {})
        self.log = logger or LOGGER
        self._invocations: list[CliInvocation] = []
        self._resolved_path: str | None = None

    # ── 路径解析 ────────────────────────────────────────────────

    def _env_value(self, key: str) -> str:
        """读取环境变量（``extra_env`` 优先，便于测试注入）。"""
        value = self.extra_env.get(key)
        if value:
            return value
        return os.environ.get(key, "")

    def _discover_candidates(self) -> list[Path]:
        """列举 npm 全局安装的常见落点（exe 优先，其次 .cmd，最后 .ps1）。"""
        stem = "tencent-channel-cli"
        dirs: list[Path] = []
        for key in ("APPDATA", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = self._env_value(key)
            if base:
                dirs.append(Path(base) / "npm")
        exes: list[Path] = []
        cmds: list[Path] = []
        for npm_dir in dirs:
            # npm >= 7 的平台包布局（本机实测）：
            #   <npm>/node_modules/tencent-channel-cli/node_modules/tencent-channel-cli-win32-x64/bin/<stem>.exe
            modules = npm_dir / "node_modules"
            if modules.is_dir():
                exes.extend(sorted(modules.glob(f"**/{stem}.exe")))
                cmds.extend(sorted(modules.glob(f"{stem}*/bin/{stem}.cmd")))
            cmds.extend([npm_dir / f"{stem}.cmd", npm_dir / f"{stem}.exe"])
        return [*exes, *cmds]

    def resolve_path(self) -> str:
        """解析实际可用的 CLI 路径（只在 ``mode=auto`` 且配置路径不可用时做一次发现）。

        Windows 上裸命令会走 ``.ps1``（执行策略受限时无限卡住），因此发现阶段
        **优先返回平台包里的 exe**，其次才是 ``.cmd``；``.ps1`` 永不选。
        """
        if self._resolved_path is not None:
            return self._resolved_path

        configured = self.path
        if configured:
            try:
                if Path(configured).is_file():
                    self._resolved_path = configured
                    return configured
            except OSError:
                pass
            found = shutil.which(configured)
            if found:
                # 用绝对路径，避免子进程（cmd /c）继承到不同的 PATH
                self.path = found
                self._resolved_path = found
                return found

        if self.mode == "auto":
            for candidate in self._discover_candidates():
                if candidate.is_file():
                    self.path = str(candidate)
                    suffix = candidate.suffix.lower()
                    if suffix == ".exe":
                        self.mode = "exe"
                    elif suffix in {".cmd", ".bat"}:
                        self.mode = "cmd"
                    elif suffix in {".js", ".mjs", ".cjs"}:
                        self.mode = "node"
                    self.log.info(f"已自动发现 tencent-channel-cli：{self.path}（mode={self.mode}）")
                    self._resolved_path = self.path
                    return self.path

        self._resolved_path = configured
        return configured

    def describe(self) -> str:
        """返回可读的「路径（调用方式）」描述，供日志使用。"""
        self.resolve_path()
        return f"{self.path}（mode={self.resolved_mode()}）"

    # ── 调用方式解析 ────────────────────────────────────────────

    def resolved_mode(self) -> str:
        """确定实际调用方式。"""
        mode = self.mode
        if mode == "auto":
            suffix = Path(self.path).suffix.lower()
            if suffix in {".js", ".mjs", ".cjs"}:
                return "node"
            if suffix == ".py":
                return "python"
            if suffix in {".cmd", ".bat"}:
                return "cmd"
            return "exe"
        return mode

    def _exec_prefix(self) -> list[str]:
        path = self.resolve_path()
        mode = self.resolved_mode()
        if mode == "node":
            node = self.node_path or shutil.which("node") or "node"
            return [node, path]
        if mode == "python":
            python = self.python_path or sys.executable
            return [python, path]
        if mode == "cmd":
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            return [comspec, "/d", "/s", "/c", path]
        return [path]

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.update(self.extra_env)
        return env

    # ── 执行 ────────────────────────────────────────────────────

    # ── 通用调用（给「CLI 能力清单」用）────────────────────────

    async def run_command(
        self,
        domain: str,
        action: str,
        params: Mapping[str, Any] | None = None,
        *,
        plain_text: bool = False,
        yes: bool = False,
    ) -> CliResult:
        """通用调用 ``tencent-channel-cli <domain> <action>``，参数走 stdin JSON。

        - ``domain="cli"`` 表示顶层命令（``version`` / ``doctor``），不带域前缀；
        - 键名先做规范化（kebab→snake、``image``→``file_paths`` 等），
          所以提示词里写 CLI 的 flag 名也能用；
        - ``yes=True`` 才会附加全局 ``--yes``（默认**永不**附加），由调用方按配置决定。
        """
        clean = normalize_params(params or {})
        top_level = domain in ("", "cli")
        return await self.execute(
            self.new_invocation(
                "" if top_level else domain,
                action,
                clean,
                flags={"yes": True} if yes else None,
                use_stdin=not (top_level and plain_text),
                allow_plain=plain_text,
            )
        )

    def new_invocation(
        self,
        domain: str,
        action: str,
        params: Mapping[str, Any] | None = None,
        *,
        flags: Mapping[str, Any] | None = None,
        use_stdin: bool = True,
        allow_plain: bool = False,
    ) -> CliInvocation:
        """构造一次调用描述（同时登记，便于排障与单测）。"""
        inv = CliInvocation(
            domain=domain,
            action=action,
            params=dict(params or {}),
            flags=dict(flags or {}),
            use_stdin=use_stdin,
            dry_run=self.dry_run,
            allow_plain=allow_plain,
        )
        self._invocations.append(inv)
        return inv

    async def execute(self, inv: CliInvocation, *, retry_on_rate_limit: bool = True) -> CliResult:
        """执行一次 CLI 调用。"""
        argv = [*self._exec_prefix(), *inv.argv_tokens()]
        started = time.monotonic()
        stdin_bytes = inv.stdin_bytes()
        self.log.debug(f"CLI 调用: {inv.preview()}")

        result = await self._spawn(argv, stdin_bytes, started)
        if inv.allow_plain and result.kind == "error" and result.returncode == 0 and result.stdout.strip():
            # version / login status 等命令可能输出纯文本，这里做兼容降级
            result = CliResult(
                kind="ok",
                ok=True,
                data={"text": result.stdout.strip()},
                argv=result.argv,
                duration_ms=result.duration_ms,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        if result.kind == "rate_limit" and retry_on_rate_limit:
            self.log.warning(
                f"CLI 限流（153），按规则 sleep {self.rate_limit_sleep:.0f}s 后原样重试一次"
            )
            await asyncio.sleep(self.rate_limit_sleep)
            result = await self._spawn(argv, stdin_bytes, started)
        return result

    async def _spawn(self, argv: Sequence[str], stdin_bytes: bytes | None, started: float) -> CliResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
        except OSError as exc:
            not_found = isinstance(exc, (FileNotFoundError, NotADirectoryError)) or getattr(
                exc, "winerror", None
            ) in (2, 3)
            return CliResult(
                kind="not_found" if not_found else "error",
                error=(
                    f"找不到可执行文件: {argv[0]} ({exc})" if not_found else f"启动 CLI 失败: {exc}"
                ),
                argv=tuple(argv),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            killer = getattr(proc, "kill", None)
            if callable(killer):
                try:
                    killer()
                except ProcessLookupError:
                    pass
            return CliResult(
                kind="timeout",
                error=f"CLI 调用超时（>{self.timeout:.0f}s）",
                argv=tuple(argv),
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        result = parse_cli_output(stdout, stderr, returncode=proc.returncode or 0)
        result.argv = tuple(argv)
        result.duration_ms = duration_ms
        result.stdout = stdout[:4000]
        result.stderr = stderr[:1000]
        result.returncode = proc.returncode or 0
        if not result.ok:
            self.log.warning(
                f"CLI 返回失败: kind={result.kind} retCode={result.ret_code} error={result.error}"
            )
        return result

    # ── 具体命令 ────────────────────────────────────────────────

    async def version(self) -> CliResult:
        """``tencent-channel-cli version``（允许纯文本输出）。"""
        return await self.execute(self.new_invocation("", "version", use_stdin=False, allow_plain=True))

    async def login_status(self) -> CliResult:
        """``tencent-channel-cli login status``（允许纯文本输出）。"""
        return await self.execute(
            self.new_invocation("login", "status", use_stdin=False, allow_plain=True)
        )

    async def doctor(self) -> CliResult:
        """``tencent-channel-cli doctor``（允许纯文本输出）。"""
        return await self.execute(self.new_invocation("", "doctor", use_stdin=False, allow_plain=True))

    async def get_notices(
        self,
        *,
        guild_id: str = "",
        page_num: int | None = None,
        attach_info: str = "",
    ) -> CliResult:
        """互动消息（评论/回复/@/点赞）。纯读取，不需要 notices-on。"""
        params: dict[str, Any] = {}
        if guild_id:
            params["guild_id"] = guild_id
        if page_num:
            params["page_num"] = page_num
        if attach_info:
            params["attach_info"] = attach_info
        return await self.execute(self.new_invocation("feed", "get-notices", params))

    async def get_feed_detail(self, feed_id: str, *, guild_id: str = "", channel_id: str = "") -> CliResult:
        """帖子详情（含 author_id / create_time，供回复时补齐字段）。"""
        params: dict[str, Any] = {"feed_id": feed_id}
        if guild_id:
            params["guild_id"] = guild_id
        if channel_id:
            params["channel_id"] = channel_id
        return await self.execute(self.new_invocation("feed", "get-feed-detail", params))

    async def get_feed_comments(
        self,
        feed_id: str,
        *,
        guild_id: str = "",
        channel_id: str = "",
        count: int = 20,
        attach_info: str = "",
        rank_type: int | None = None,
        reply_list_num: int | None = None,
    ) -> CliResult:
        """帖子评论列表（count 上限 20；rank_type 2=时间倒序；reply_list_num 预加载回复数，上限 10）。"""
        params: dict[str, Any] = {"feed_id": feed_id, "count": count}
        if guild_id:
            params["guild_id"] = guild_id
        if channel_id:
            params["channel_id"] = channel_id
        if attach_info:
            params["attach_info"] = attach_info
        if rank_type is not None:
            params["rank_type"] = rank_type
        if reply_list_num is not None:
            params["reply_list_num"] = reply_list_num
        return await self.execute(self.new_invocation("feed", "get-feed-comments", params))

    async def search_feeds(
        self,
        query: str,
        *,
        guild_id: str,
        next_page_cookie: str = "",
    ) -> CliResult:
        """频道内搜索帖子（要求已加入该频道）。"""
        params: dict[str, Any] = {"guild_id": guild_id, "query": query}
        if next_page_cookie:
            params["cookie"] = next_page_cookie
        return await self.execute(self.new_invocation("feed", "search-guild-feeds", params))

    async def get_guild_info(self, guild_id: str) -> CliResult:
        """频道基本信息。"""
        return await self.execute(self.new_invocation("manage", "get-guild-info", {"guild_id": guild_id}))

    async def get_channel_list(self, guild_id: str) -> CliResult:
        """频道版块列表。"""
        return await self.execute(self.new_invocation("manage", "get-guild-channel-list", {"guild_id": guild_id}))

    async def get_my_guilds(self) -> CliResult:
        """我加入/创建/管理的频道列表（用于取真实 guild_id）。"""
        return await self.execute(self.new_invocation("manage", "get-my-join-guild-info", use_stdin=False))

    async def get_guild_feeds(
        self,
        *,
        guild_id: str,
        get_type: int = 2,
        count: int = 10,
        feed_attach_info: str = "",
    ) -> CliResult:
        """频道主页帖子列表（``get_type``: 1=热门 2=最新），用于发现新帖子。"""
        params: dict[str, Any] = {"guild_id": guild_id, "get_type": get_type, "count": count}
        if feed_attach_info:
            params["feed_attach_info"] = feed_attach_info
        return await self.execute(self.new_invocation("feed", "get-guild-feeds", params))

    async def search_guild_content(
        self, keyword: str, *, scope: str = "channel", next_page_token: str = ""
    ) -> CliResult:
        """搜索腾讯频道/帖子/作者（``scope=channel`` 时可把频道号换成真实 guild_id）。"""
        params: dict[str, Any] = {"keyword": keyword, "scope": scope or "channel"}
        if next_page_token:
            params["next_page_token"] = next_page_token
        return await self.execute(self.new_invocation("manage", "search-guild-content", params))

    async def search_members(self, keyword: str, *, guild_id: str, num: int = 20) -> CliResult:
        """搜索频道成员（拿 tiny_id）。"""
        params: dict[str, Any] = {"guild_id": guild_id, "keyword": keyword, "num": num}
        return await self.execute(self.new_invocation("manage", "guild-member-search", params))

    # ── 登录 / 退登（凭证生命周期）─────────────────────────

    async def login_start(self) -> CliResult:
        """发起扫码授权（``login --json``）：立即返回授权链接与二维码，不阻塞。"""
        return await self.execute(self.new_invocation("login", "", use_stdin=False))

    async def login_poll_token(self) -> CliResult:
        """等待扫码完成（``login poll-token --json``）；服务端最长轮询 10 分钟，受 cli.timeout 约束。"""
        return await self.execute(self.new_invocation("login", "poll-token", use_stdin=False))

    async def login_logout(self) -> CliResult:
        """清除本机所有登录凭证（``login logout --yes``：密钥链 + .env + 二维码缓存）。"""
        return await self.execute(
            self.new_invocation("login", "logout", flags={"yes": True}, use_stdin=False)
        )

    async def get_user_info(self, **params: Any) -> CliResult:
        """查看用户资料（字段名以 ``schema manage.get-user-info`` 为准）。"""
        return await self.execute(self.new_invocation("manage", "get-user-info", params))

    # ── 写操作（非破坏性，type 固定为 1，永不传 --yes）───────

    async def publish_feed(
        self,
        content: str = "",
        *,
        guild_id: str = "",
        channel_id: str = "",
        title: str = "",
        feed_type: int | None = None,
        markdown_content: str = "",
        content_file: str = "",
        at_users: Sequence[Mapping[str, str]] | None = None,
        topic_names: Sequence[str] | None = None,
        links: Sequence[Mapping[str, str]] | None = None,
        image_paths: Sequence[str] | None = None,
        video_paths: Sequence[str] | None = None,
    ) -> CliResult:
        """发帖：短贴/长贴、纯文本或 Markdown、可带图片/视频/话题/@/文字链接。

        - ``title`` 非空即长贴（≤10000 字），否则短贴（≤1000 字）
        - ``markdown_content`` 仅长贴可用，且与 ``content`` 互斥
        - ``content_file`` 与 ``content`` / ``markdown_content`` 互斥
        - 图片：``file_paths``（短贴≤18 / 长贴≤50）；视频：``video_paths``（短贴 1 / 长贴 5）
        """
        params: dict[str, Any] = {}
        if content:
            params["content"] = content
        if markdown_content:
            params["markdown_content"] = markdown_content
        if content_file:
            params["content_file"] = content_file
        if guild_id:
            params["guild_id"] = guild_id
        if channel_id:
            params["channel_id"] = channel_id
        if title:
            params["title"] = title
        if feed_type is not None:
            params["feed_type"] = 2 if int(feed_type) == 2 else 1
        if at_users:
            params["at_users"] = [dict(u) for u in at_users]
        if topic_names:
            params["topic_names"] = [str(t) for t in topic_names if str(t).strip()]
        if links:
            params["urls"] = [dict(u) for u in links]
        if image_paths:
            params["file_paths"] = [
                {"file_path": str(p)} for p in image_paths if str(p).strip()
            ]
        if video_paths:
            params["video_paths"] = [
                {"file_path": str(p)} for p in video_paths if str(p).strip()
            ]
        return await self.execute(self.new_invocation("feed", "publish-feed", params))

    async def do_comment(
        self,
        content: str,
        *,
        feed_id: str,
        feed_create_time: Any = "",
        guild_id: str = "",
        channel_id: str = "",
        at_users: Sequence[Mapping[str, str]] | None = None,
        image_path: str = "",
    ) -> CliResult:
        """评论帖子（``comment_type=1`` 发表，绝不传 0/2）。

        ``image_path`` 可选（最多 1 张，CLI 会自动上传）；给了图片时 ``content`` 可以留空。
        """
        params: dict[str, Any] = {
            "content": content,
            "feed_id": feed_id,
            "comment_type": 1,
        }
        if feed_create_time not in ("", None):
            params["feed_create_time"] = feed_create_time
        if guild_id:
            params["guild_id"] = guild_id
        if channel_id:
            params["channel_id"] = channel_id
        if at_users:
            params["at_users"] = [dict(u) for u in at_users]
        if image_path:
            params["image_path"] = image_path
        return await self.execute(self.new_invocation("feed", "do-comment", params))

    async def do_reply(
        self,
        content: str,
        *,
        feed_id: str,
        comment_id: str,
        extra_fields: Mapping[str, Any] | None = None,
        guild_id: str = "",
        channel_id: str = "",
        image_path: str = "",
    ) -> CliResult:
        """楼中楼回复评论（``reply_type=1`` 发表，绝不传 0/2）。

        ``do-reply`` 必填字段较多（feed_author_id / feed_create_time / comment_author_id /
        comment_create_time / replier_id 等），由调用方通过 ``extra_fields`` 补齐。
        ``image_path`` 可选（最多 1 张，CLI 会自动上传）。
        """
        params: dict[str, Any] = {
            "content": content,
            "feed_id": feed_id,
            "comment_id": comment_id,
            "reply_type": 1,
        }
        for key, value in (extra_fields or {}).items():
            if value in ("", None):
                continue
            params[key] = value
        if guild_id:
            params["guild_id"] = guild_id
        if channel_id:
            params["channel_id"] = channel_id
        if image_path:
            params["image_path"] = image_path
        return await self.execute(self.new_invocation("feed", "do-reply", params))

    async def do_feed_prefer(
        self,
        feed_id: str,
        *,
        action: int = 1,
        guild_id: str = "",
        channel_id: str = "",
    ) -> CliResult:
        """帖子点赞（``action=1``）/ 取消点赞（``action=3``）。

        只允许 1 / 3 两个取值，永不传 ``--yes``。
        """
        params: dict[str, Any] = {
            "feed_id": feed_id,
            "action": 3 if int(action) == 3 else 1,
        }
        if guild_id:
            params["guild_id"] = guild_id
        if channel_id:
            params["channel_id"] = channel_id
        return await self.execute(self.new_invocation("feed", "do-feed-prefer", params))

    async def do_like(
        self,
        *,
        feed_id: str,
        comment_id: str,
        feed_author_id: str = "",
        feed_create_time: Any = "",
        comment_author_id: str = "",
        reply_id: str = "",
        reply_author_id: str = "",
        cancel: bool = False,
        guild_id: str = "",
        channel_id: str = "",
    ) -> CliResult:
        """评论/回复点赞。

        ``like_type``：3=赞评论 / 4=取消评论赞 / 5=赞回复 / 6=取消回复赞——
        只允许这四个"点赞/取消自己点赞"的取值，永不传删除类参数，也不传 ``--yes``。
        """
        if reply_id:
            like_type = 6 if cancel else 5
        else:
            like_type = 4 if cancel else 3
        params: dict[str, Any] = {
            "like_type": like_type,
            "feed_id": feed_id,
            "comment_id": comment_id,
        }
        if feed_author_id:
            params["feed_author_id"] = feed_author_id
        if feed_create_time not in ("", None):
            params["feed_create_time"] = feed_create_time
        if comment_author_id:
            params["comment_author_id"] = comment_author_id
        if reply_id:
            params["reply_id"] = reply_id
        if reply_author_id:
            params["reply_author_id"] = reply_author_id
        if guild_id:
            params["guild_id"] = guild_id
        if channel_id:
            params["channel_id"] = channel_id
        return await self.execute(self.new_invocation("feed", "do-like", params))

    async def push_dm(
        self,
        text: str,
        *,
        peer_tiny_id: str = "",
        source_guild_id: str = "",
        ref: int | None = None,
    ) -> CliResult:
        """私信。

        - 回复已收到的私信通知：``ref``（需要 notices-on，本插件默认不用）
        - 主动私信：``peer_tiny_id`` + ``source_guild_id``（发送方所属频道）
        """
        if ref is not None:
            return await self.execute(
                self.new_invocation("manage", "push-group-dm-msg", {"ref": ref, "text": text})
            )
        params: dict[str, Any] = {"text": text}
        if peer_tiny_id:
            params["peer_tiny_id"] = peer_tiny_id
        if source_guild_id:
            params["source_guild_id"] = source_guild_id
        return await self.execute(self.new_invocation("manage", "push-group-dm-msg", params))

    # ── 诊断 ────────────────────────────────────────────────────

    @property
    def invocations(self) -> tuple[CliInvocation, ...]:
        """已发生的调用记录（只读，排障/测试用）。"""
        return tuple(self._invocations)


# ── 参数名规范化（纯函数，便于单测）─────────────────────────

#: flag 名 → stdin JSON 键名的特殊映射（其余只做 kebab→snake）
PARAM_ALIASES: dict[str, str] = {
    "image": "file_paths",
    "images": "file_paths",
    "file_path": "file_paths",
    "video": "video_paths",
    "videos": "video_paths",
    "link": "urls",
    "links": "urls",
    "at_user": "at_users",
    "topic_name": "topic_names",
    "attach_info": "attach_info",
    "ref": "ref",
}


def normalize_param_name(name: str) -> str:
    """把 CLI 的 flag 名规范成 stdin JSON 键名。"""
    text = str(name or "").strip().lstrip("-").replace("-", "_").lower()
    return PARAM_ALIASES.get(text, text)


def normalize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """规范化一整套参数（键名 + 去掉空值）。"""
    result: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None or value == "" or value == []:
            continue
        result[normalize_param_name(str(key))] = value
    return result


# ── 发帖参数整形与本地文件校验（纯函数，便于单测）─────────────


def parse_at_users(items: Iterable[str] | None) -> list[dict[str, str]]:
    """把 ``"tinyid:昵称"`` 列表整形成 CLI 的 ``at_users``。"""
    out: list[dict[str, str]] = []
    for raw in items or ():
        text = str(raw)
        tiny_id, _, nick = text.partition(":")
        tiny_id = tiny_id.strip()
        if tiny_id:
            out.append({"id": tiny_id, "nick": nick.strip()})
    return out


def parse_links(items: Iterable[str] | None) -> list[dict[str, str]]:
    """把 ``"url|显示文字"`` 列表整形成 CLI 的 ``urls``。"""
    out: list[dict[str, str]] = []
    for raw in items or ():
        text = str(raw)
        url, _, label = text.partition("|")
        url = url.strip()
        if url:
            out.append({"url": url, "displayText": label.strip()})
    return out


def check_local_files(paths: Iterable[str] | None) -> tuple[list[str], str]:
    """校验图片/视频路径：必须存在且是文件。返回 ``(绝对路径列表, 错误)``。"""
    out: list[str] = []
    for raw in paths or ():
        text = str(raw).strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            return [], f"文件不存在：{text}"
        out.append(str(path))
    return out, ""


def check_content_file(raw: str, *, allowed_root: str | Path | None = None) -> tuple[str, str]:
    """校验 ``content_file``：只允许读实例 ``data/`` 下的 txt/md。

    安全边界：正文文件是从本地磁盘**读进公开帖子**的通道，所以限制在实例数据目录内，
    避免被频道里的陌生人诱导读到任意本地文件。返回 ``(绝对路径, 错误)``。
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.is_file():
        return "", f"正文文件不存在：{text}"
    if path.suffix.lower() not in (".txt", ".md"):
        return "", "content_file 只支持 .txt / .md 文本文件"
    root = Path(allowed_root) if allowed_root is not None else (Path.cwd() / "data")
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return "", (
            f"出于安全边界，content_file 只允许读实例 data/ 目录下的文件（收到：{path}）。"
            "请先把文件放进 data/ 再发帖。"
        )
    return str(path), ""


# ── 输出解析（纯函数，便于单测）─────────────────────────────


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从 CLI 输出里提取 JSON 对象。

    ``--json`` 正常只输出一个 JSON，但可能夹带日志行；这里做逐级回退：
    整体 → 首个 ``{`` 到末个 ``}`` → 逐行倒序。
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _first_int(mapping: Mapping[str, Any], keys: Iterable[str]) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def classify(ret_code: int | None, error_text: str) -> str:
    """按 retCode / 错误文本分类，供调用方分支处理。

    表里的 retCode 与文案都来自生产日志实测（151 / 8010 / 10014 / wsarecv），
    不确定的新形态一律落到 ``business``（只透出原始错误，不猜建议）。
    """
    text = error_text or ""
    lowered = text.lower()
    if ret_code in RATE_LIMIT_RET_CODES or any(m in text for m in RATE_LIMIT_MARKERS):
        return "rate_limit"
    if ret_code in AUTH_RET_CODES or any(m in text for m in AUTH_MARKERS):
        return "auth"
    if ret_code in JOIN_REQUIRED_RET_CODES:
        return "join_required"
    if ret_code in DM_LIMIT_RET_CODES or "100707" in lowered:
        return "dm_limit"
    if ret_code in INVALID_PARAM_RET_CODES:
        return "invalid_param"
    if ret_code in GONE_RET_CODES or any(m in lowered for m in GONE_MARKERS):
        return "gone"
    if any(m in lowered for m in NETWORK_MARKERS):
        return "network"
    if ret_code not in (None, 0):
        return "business"
    if text:
        return "business"
    return "ok"


def parse_cli_output(stdout: str, stderr: str = "", *, returncode: int = 0) -> CliResult:
    """解析 ``--json`` 输出为 :class:`CliResult`（纯函数，便于单测）。"""
    obj = extract_json_object(stdout)
    if obj is None:
        # 可能是失败的 Human 输出（写操作不带 --json 时是纯文本）
        text = stdout or stderr
        if returncode != 0:
            return CliResult(
                kind="error",
                error=f"CLI 以退出码 {returncode} 结束: {text[:500]}",
                ret_code=_ret_from_text(text),
                stderr=stderr[:1000],
            )
        return CliResult(
            kind="error",
            error=f"无法解析 CLI 输出为 JSON: {text[:500]}",
            stderr=stderr[:1000],
        )

    success = bool(obj.get("success", False))
    data = obj.get("data")
    error_obj = obj.get("error")
    error_text = ""
    if isinstance(error_obj, dict):
        error_text = str(error_obj.get("message") or error_obj.get("msg") or "")
        if not error_text:
            error_text = json.dumps(error_obj, ensure_ascii=False)
    elif isinstance(error_obj, str):
        error_text = error_obj

    ret_code: int | None = None
    if isinstance(data, dict):
        ret_code = _first_int(data, ("retCode", "ret_code", "code", "retcode"))
        inner_msg = data.get("retMsg") or data.get("ret_msg")
        if isinstance(inner_msg, str) and inner_msg and ret_code not in (None, 0):
            error_text = error_text or inner_msg
    if ret_code is None:
        ret_code = _first_int(obj, ("retCode", "ret_code", "code"))
    if ret_code is None:
        ret_code = _ret_from_text(error_text) or _ret_from_text(stdout)

    kind = classify(ret_code, error_text)
    ok = success and ret_code in (None, 0) and kind == "ok"
    if not ok and kind == "ok":
        kind = "business"
    if ok:
        kind = "ok"

    return CliResult(
        kind=kind,
        ok=ok,
        data=data,
        raw=obj if isinstance(obj, dict) else {},
        error="" if ok else (error_text or f"CLI 返回成功标志为 false（retCode={ret_code}）"),
        ret_code=ret_code,
        stderr=stderr[:1000],
    )


def _ret_from_text(text: str) -> int | None:
    match = _RET_CODE_RE.search(text or "")
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None
