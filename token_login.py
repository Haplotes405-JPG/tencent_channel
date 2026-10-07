"""Token 直登：把配置里的访问令牌写进 CLI 的凭证存储，让 CLI 免扫码恢复登录。

``tencent-channel-cli`` 把访问令牌存在**系统密钥环**（Windows 凭据管理器条目
``qq-cli:token`` / ``qq-cli:device-id``，``cmdkey /list`` 可见）。CLI 本身没有
「用令牌登录」的命令，本模块直接把配置里的令牌写回它的存储位：写完后
``login status`` 即恢复 ``tokenSource=keychain``，与扫码登录后的形态**完全一致**
（Windows 已实测）。

跨平台策略（CLI 官方有 linux-x64 / linux-arm64 / darwin 构建）：

- **Windows**：凭据管理器 ctypes 读写（生产验证）；
- **Linux**：优先 ``secret-tool``（libsecret，与 CLI 的 go-keyring 同一套
  Secret Service 属性 ``service=qq-cli / username=token``）；
- **macOS**：``security add-generic-password``（钥匙串，service/account 同名）；
- **POSIX 兜底**：密钥环不可用（无桌面的服务器）时写 CLI 自己的 dotenv 降级文件
  ``~/.qqcli/.env``（CLI 读不到密钥环就读它，doctor 的提示原文；路径可用环境变量
  ``QQ_AI_CONNECT_DOTENV`` 重定向）。

⚠️ Linux/macOS 两条路径按 go-keyring 的存储约定实现，尚未在真机验证；
Windows 路径已实测。

适配器的使用策略是「**兜底**」：只在 ``login status`` 失败时注入，绝不覆盖一个
已经有效的登录（比如刚扫码产生的新令牌）。典型用法：

1. 扫码登录成功后跑一次 ``export_login_token.py``，把密钥环里的令牌导出成 JSON；
2. 把文件路径填进 ``cli.login.token_file``；
3. 之后每次重启（包括 ``cli.login.logout_on_startup=true`` 清掉凭证后）适配器自动用令牌
   恢复登录，不再需要手机扫码。

安全约定：

- 令牌进日志只允许**掩码**形态（:func:`mask_secret`）；
- 本模块不提供任何 LLM 可调用的入口，令牌不经过工具/动作层；
- 推荐用 ``cli.login.token_file`` 而不是 ``cli.login.token``（明文不进 config.toml）。

本模块保持**纯标准库**、无框架依赖，可直接被离线回归脚本按文件加载。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: CLI 内部 keychain 服务名与账号（zalando/go-keyring 的凭据目标为
#: ``<service>:<username>``，实测即 ``qq-cli:token`` / ``qq-cli:device-id``）
KEYCHAIN_SERVICE = "qq-cli"
TOKEN_ACCOUNT = "token"
DEVICE_ACCOUNT = "device-id"

#: dotenv 降级文件的键名（与 CLI 自身 .env 降级格式同名，见 CLI 二进制内的
#: QQ_AI_CONNECT_* 环境变量字符串）
DOTENV_KEYS: dict[str, str] = {
    TOKEN_ACCOUNT: "QQ_AI_CONNECT_TOKEN",
    DEVICE_ACCOUNT: "QQ_AI_CONNECT_DEVICE_ID",
}

_CRED_TYPE_GENERIC = 1
#: 与 go-keyring 一致的持久级别（新条目用；覆盖已有条目时沿用原级别）
_CRED_PERSIST_ENTERPRISE = 3


class TokenLoginError(RuntimeError):
    """令牌解析或注入失败（文件缺失 / 无可用凭证存储 / 凭据 API 报错）。"""


@dataclass
class LoginCredential:
    """一条待注入的登录凭证。"""

    token: str
    device_id: str = ""
    #: 日志用：令牌来自哪个配置项（token / token_file:<路径>）
    source: str = ""

    @property
    def masked(self) -> str:
        return mask_secret(self.token)


def mask_secret(secret: str, keep: int = 6) -> str:
    """令牌掩码：只露前 ``keep`` 位并标注总长，空串原样返回。"""
    secret = secret or ""
    if not secret:
        return ""
    if len(secret) <= keep:
        return "…（%d 位）" % len(secret)
    return f"{secret[:keep]}…（共 {len(secret)} 位）"


# ── 配置解析 ──────────────────────────────────────────────


def _cfg_get(config: Any, section: str, field: str, default: Any) -> Any:
    """与 cli_service.cfg_get 等价的本地副本（保持本模块可独立加载/测试）。

    ``section`` 同样支持点号路径（登录配置在 ``[cli.login]`` 节）。
    """
    node: Any = config
    for part in str(section).split("."):
        node = getattr(node, part, None) if node is not None else None
        if node is None:
            return default
    value = getattr(node, field, default)
    return default if value is None else value


def resolve_login_credential(config: Any) -> LoginCredential | None:
    """从 ``[cli]`` 配置解析 token 直登凭证；两项都未配置时返回 ``None``。

    优先级：``cli.login.token_file`` > ``cli.login.token``。文件支持三种形态：
    * JSON：``{"token": "...", "device_id": "..."}``
    * dotenv 行：``QQ_AI_CONNECT_TOKEN=...``（兼容 CLI 自身 .env 降级格式）
    * 纯文本：整个文件（去首尾空白）当作 token
    配置了但解析不出来时抛 :class:`TokenLoginError`——静默跳过会让人误以为生效。
    """
    file_path = str(_cfg_get(config, "cli.login", "token_file", "") or "").strip()
    inline = str(_cfg_get(config, "cli.login", "token", "") or "").strip()
    device_id = str(_cfg_get(config, "cli.login", "device_id", "") or "").strip()

    if file_path:
        text = _read_token_file(file_path)
        token, file_device = _parse_token_text(text)
        token = token.strip()
        if not token:
            raise TokenLoginError(f"cli.login.token_file 里没有可用令牌：{file_path}")
        return LoginCredential(
            token=token,
            device_id=device_id or file_device,
            source=f"token_file:{file_path}",
        )
    if inline:
        return LoginCredential(token=inline, device_id=device_id, source="token")
    return None


def _read_token_file(file_path: str) -> str:
    """读取令牌文件（支持 ``~`` 与环境变量展开），读不到抛 TokenLoginError。"""
    expanded = os.path.expandvars(os.path.expanduser(file_path))
    try:
        return Path(expanded).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TokenLoginError(f"cli.login.token_file 读取失败（{file_path}）：{exc}") from exc


def _parse_token_text(text: str) -> tuple[str, str]:
    """把令牌文件内容解析成 ``(token, device_id)``；识别 JSON / dotenv / 纯文本。"""
    text = (text or "").strip()
    if not text:
        return "", ""
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            token = str(payload.get("token", "") or "").strip()
            device = str(payload.get("device_id", "") or payload.get("device-id", "") or "").strip()
            return token, device
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("QQ_AI_CONNECT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'"), ""
    return text, ""


# ── Windows 凭据管理器（ctypes advapi32）──────────────────────


class _CREDENTIAL(ctypes.Structure):
    """wincred.CREDENTIAL（x64 布局，与 advapi32 的 CREDENTIALW 一致）。"""

    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_void_p),
        ("LastWritten", ctypes.c_uint64),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.c_char_p),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


_advapi32: Any = None
if os.name == "nt":
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.CredReadW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.CredReadW.restype = ctypes.c_bool
    _advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), ctypes.c_uint32]
    _advapi32.CredWriteW.restype = ctypes.c_bool
    _advapi32.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    _advapi32.CredDeleteW.restype = ctypes.c_bool
    _advapi32.CredFree.argtypes = [ctypes.c_void_p]
    _advapi32.CredFree.restype = None


def _target(account: str) -> str:
    return f"{KEYCHAIN_SERVICE}:{account}"


def _read_cred_raw(target: str) -> tuple[bytes, int, str] | None:
    """读原始条目，返回 ``(blob, persist, username)``；不存在返回 None。仅 Windows。"""
    ptr = ctypes.c_void_p()
    if not _advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ctypes.cast(ptr, ctypes.POINTER(_CREDENTIAL)).contents
        size = int(cred.CredentialBlobSize)
        blob = b""
        if size and cred.CredentialBlob:
            buf = ctypes.create_string_buffer(size)
            ctypes.memmove(buf, cred.CredentialBlob, size)
            blob = buf.raw[:size]
        return blob, int(cred.Persist), str(cred.UserName or "")
    finally:
        _advapi32.CredFree(ptr)


def _win_write(account: str, secret: str) -> str:
    """写入/覆盖 Windows 凭据管理器条目；已存在时沿用原 Persist 与 UserName。"""
    target = _target(account)
    secret_bytes = secret.encode("utf-8")
    existing = _read_cred_raw(target)
    persist = existing[1] if existing else _CRED_PERSIST_ENTERPRISE
    username = existing[2] if existing and existing[2] else account

    blob = ctypes.create_string_buffer(secret_bytes, len(secret_bytes))
    cred = _CREDENTIAL(
        Flags=0,
        Type=_CRED_TYPE_GENERIC,
        TargetName=target,
        Comment=None,
        LastWritten=0,
        CredentialBlobSize=len(secret_bytes),
        CredentialBlob=ctypes.cast(blob, ctypes.c_char_p),
        Persist=persist,
        AttributeCount=0,
        Attributes=None,
        TargetAlias=None,
        UserName=username,
    )
    if not _advapi32.CredWriteW(ctypes.byref(cred), 0):
        code = ctypes.get_last_error()
        raise TokenLoginError(f"写入 Windows 凭据管理器失败（{target}）：WinError {code}")
    return target


# ── POSIX：系统密钥环（secret-tool / security）+ dotenv 降级 ─────────


def _run_quiet(argv: list[str], *, stdin: bytes | None = None, timeout: float = 10.0) -> subprocess.CompletedProcess[bytes]:
    """跑一条密钥环 CLI；不存在/超时按失败处理（调用方决定是否走 dotenv 兜底）。"""
    return subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout)  # noqa: S603


def _linux_read(account: str) -> str | None:
    """读 Secret Service（secret-tool）；工具缺失/服务不可达返回 None。"""
    tool = shutil.which("secret-tool")
    if not tool:
        return None
    try:
        proc = _run_quiet([tool, "lookup", "service", KEYCHAIN_SERVICE, "username", account])
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def _linux_write(account: str, secret: str) -> str:
    """写 Secret Service；工具缺失或写入失败抛 TokenLoginError（由调用方兜底）。"""
    tool = shutil.which("secret-tool")
    if not tool:
        raise TokenLoginError("未找到 secret-tool（安装 libsecret-tools 后可用系统密钥环）")
    try:
        proc = _run_quiet(
            [tool, "store", "--label", f"{KEYCHAIN_SERVICE}:{account}",
             "service", KEYCHAIN_SERVICE, "username", account],
            stdin=secret.encode("utf-8"),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TokenLoginError(f"secret-tool 写入失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise TokenLoginError(f"secret-tool 写入失败（rc={proc.returncode}）：{detail or 'Secret Service 不可用'}")
    return f"secret-service:{KEYCHAIN_SERVICE}:{account}"


def _linux_delete(account: str) -> bool:
    tool = shutil.which("secret-tool")
    if not tool:
        return False
    try:
        return _run_quiet([tool, "clear", "service", KEYCHAIN_SERVICE, "username", account]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _macos_read(account: str) -> str | None:
    """读 macOS 钥匙串（security CLI，service/account 与 go-keyring 同名）。"""
    if not shutil.which("security"):
        return None
    try:
        proc = _run_quiet(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"])
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def _macos_write(account: str, secret: str) -> str:
    """写 macOS 钥匙串；失败抛 TokenLoginError（由调用方兜底）。

    ⚠️ ``security`` CLI 只能经 argv 传密码，写入瞬间本进程 argv 对同机其他用户可见。
    """
    if not shutil.which("security"):
        raise TokenLoginError("未找到 security CLI（macOS 自带，勿改动 PATH）")
    try:
        proc = _run_quiet(
            ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w", secret, "-U"]
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TokenLoginError(f"security 写入失败：{exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise TokenLoginError(f"security 写入失败（rc={proc.returncode}）：{detail}")
    return f"keychain:{KEYCHAIN_SERVICE}:{account}"


def _macos_delete(account: str) -> bool:
    if not shutil.which("security"):
        return False
    try:
        return _run_quiet(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _dotenv_path() -> Path:
    """CLI 的 dotenv 降级文件路径（``QQ_AI_CONNECT_DOTENV`` 可重定向）。"""
    override = os.environ.get("QQ_AI_CONNECT_DOTENV", "").strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    return Path.home() / ".qqcli" / ".env"


def _dotenv_read(account: str) -> str | None:
    """读 dotenv 降级文件里对应键（CLI 密钥环不可用时的凭证来源）。"""
    key = DOTENV_KEYS.get(account)
    path = _dotenv_path()
    if not key or not path.is_file():
        return None
    try:
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        return None
    return None


def _dotenv_write(account: str, secret: str) -> str:
    """把键写进/更新 dotenv 降级文件（保留其它行），返回写出的目标描述。"""
    key = DOTENV_KEYS.get(account)
    if not key:
        raise TokenLoginError(f"未知账号名（无法映射到 dotenv 键）：{account}")
    path = _dotenv_path()
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    except OSError:
        lines = []
    new_line = f"{key}={secret}"
    replaced = False
    out: list[str] = []
    for raw in lines:
        if raw.strip().startswith(f"{key}="):
            out.append(new_line)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(new_line)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        if os.name == "posix":
            os.chmod(path, 0o600)
    except OSError as exc:
        raise TokenLoginError(f"写 dotenv 降级文件失败（{path}）：{exc}") from exc
    return f"dotenv:{path}"


# ── 平台分发（对外入口；平台常量做成模块属性，便于离线测试模拟 POSIX 分支）──

_os_name = os.name
_sys_platform = sys.platform


def _native_read(account: str) -> str | None:
    """读系统密钥环；不支持的平台或读不到返回 None。"""
    if _os_name == "nt":
        raw = _read_cred_raw(_target(account))
        return raw[0].decode("utf-8", "replace") if raw else None
    if _sys_platform == "darwin":
        return _macos_read(account)
    if _sys_platform.startswith("linux"):
        return _linux_read(account)
    return None


def cred_read(account: str) -> str | None:
    """读凭证明文（原生密钥环优先，POSIX 上再查 dotenv 降级文件）。

    ⚠️ 返回的是**明文凭证**：只供导出脚本/诊断用，不得整段进日志。
    """
    native = _native_read(account)
    if native:
        return native
    if _os_name != "nt":
        return _dotenv_read(account)
    return None


def cred_write(account: str, secret: str) -> str:
    """写入/覆盖凭证，返回凭据目标名（日志用）。

    Windows 走凭据管理器（生产验证）；Linux/macOS 先试系统密钥环，不可用时
    降级到 CLI 自己的 dotenv 文件（``~/.qqcli/.env``——CLI 读不到密钥环就读它）。
    Windows 上不做 dotenv 降级：CLI 在密钥环可用时忽略 .env，写了也没用。
    """
    if not (secret or "").strip():
        raise TokenLoginError(f"拒绝写入空令牌：{account}")
    if _os_name == "nt":
        return _win_write(account, secret)
    try:
        if _sys_platform == "darwin":
            return _macos_write(account, secret)
        if _sys_platform.startswith("linux"):
            return _linux_write(account, secret)
    except TokenLoginError:
        return _dotenv_write(account, secret)
    raise TokenLoginError(f"不支持的平台（os={_os_name!r}, platform={_sys_platform!r}），无法注入令牌")


def cred_delete(account: str) -> bool:
    """删除凭证条目（测试/清理用）；不存在或平台不支持返回 False。"""
    if _os_name == "nt":
        return bool(_advapi32.CredDeleteW(_target(account), _CRED_TYPE_GENERIC, 0))
    if _sys_platform == "darwin":
        return _macos_delete(account)
    if _sys_platform.startswith("linux"):
        return _linux_delete(account)
    return False


def inject_login_credential(
    credential: LoginCredential,
    *,
    token_account: str = TOKEN_ACCOUNT,
    device_account: str = DEVICE_ACCOUNT,
) -> list[str]:
    """把凭证写入 CLI 的凭证存储位；返回写入的凭据目标名（日志用）。

    ``token_account`` / ``device_account`` 仅供离线测试重定向目标，
    运行路径一律用默认值（即 CLI 自己的 ``qq-cli:token`` / ``qq-cli:device-id``）。
    """
    written = [cred_write(token_account, credential.token)]
    if credential.device_id:
        written.append(cred_write(device_account, credential.device_id))
    return written


def read_stored_login() -> tuple[str, str]:
    """读当前凭证存储里的 ``(token, device_id)``（缺失为空串）；导出脚本/诊断用。"""
    return cred_read(TOKEN_ACCOUNT) or "", cred_read(DEVICE_ACCOUNT) or ""
