"""从本机密钥链导出腾讯频道 CLI 的登录令牌（token 直登引导脚本，**手工运行**）。

典型流程（一次扫码，之后免扫码）：

1. 扫码登录成功后（``login status --json`` 为 valid）运行本脚本：
   ::

       python plugins\\tencent_channel\\export_login_token.py
       python plugins\\tencent_channel\\export_login_token.py D:\\secret\\tc_token.json

   默认输出到当前目录 ``tencent_channel_login_token.json``。
2. 把文件路径填进 ``config.toml``（支持 ``~`` 与环境变量）：
   ::

       [cli.login]
       token_file = "D:/secret/tc_token.json"

3. 之后每次重启，适配器发现未登录就会自动用这份令牌恢复登录，
   ``login logout``（cli.login.logout_on_startup / cli.login.logout_on_shutdown）清掉密钥链也不要紧。

⚠️ **同机多实例要先看这里**：本机密钥链（``qq-cli:token``）是**全机器共享**的，
默认导出的是「当前机器上最后一次登录的那个账号」—— 同机另一个 bot 实例登录过，
就会导出**它**的令牌（串号）。此时请改用 ``--from-config``，从**本实例**的
``config.toml`` 里读令牌来导出：
::

    python export_login_token.py --from-config config/plugins/tencent_channel/config.toml my_token.json

⚠️ 输出文件**等同登录凭证**（能以你的账号身份操作频道），注意保管，
不要提交进仓库或发给别人。不落盘可改用 ``--stdout``。

本脚本独立运行（不依赖框架），也可作为包内模块导入复用其输出逻辑。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import tomllib

try:  # 作为包内模块 / 离线脚本被导入
    from .token_login import (
        LoginCredential,
        TokenLoginError,
        mask_secret,
        read_stored_login,
        resolve_login_credential,
    )
except ImportError:  # 直接以脚本运行：sys.path[0] 即插件目录
    from token_login import (  # type: ignore
        LoginCredential,
        TokenLoginError,
        mask_secret,
        read_stored_login,
        resolve_login_credential,
    )

DEFAULT_OUTPUT = "tencent_channel_login_token.json"
#: ``--from-config`` 不传路径时的默认位置（相对当前工作目录）
DEFAULT_CONFIG = "config/plugins/tencent_channel/config.toml"


def export_payload() -> dict[str, str]:
    """读密钥链并组装导出 JSON（供 CLI 与测试复用）。"""
    token, device_id = read_stored_login()
    if not token:
        raise TokenLoginError(
            "密钥链里没有找到登录令牌（qq-cli:token）。请先完成一次扫码登录："
            "`tencent-channel-cli.cmd login --json`（或把 cli.login.auto_qrcode 打开后重启）"
        )
    payload: dict[str, str] = {"token": token}
    if device_id:
        payload["device_id"] = device_id
    return payload


def export_payload_from_config(config_path: str | os.PathLike[str]) -> dict[str, str]:
    """从插件 ``config.toml`` 的 ``[cli.login]`` 里读令牌并组装导出 JSON。

    多实例机器上用它代替密钥链导出：各实例只导出**自己配置里**的令牌。
    ``token`` 优先，其次跟随 ``token_file``（复用 token_login 的解析：JSON / dotenv / 纯文本）。
    """
    path = Path(os.path.expandvars(os.path.expanduser(str(config_path))))
    if not path.is_file():
        raise TokenLoginError(f"配置文件不存在：{path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TokenLoginError(f"读取配置文件失败（{path}）：{exc}") from exc

    login = ((raw.get("cli") or {}).get("login") or {}) if isinstance(raw, dict) else {}
    shim = SimpleNamespace(
        cli=SimpleNamespace(
            login=SimpleNamespace(
                token=str(login.get("token") or ""),
                token_file=str(login.get("token_file") or ""),
                device_id=str(login.get("device_id") or ""),
            )
        )
    )
    credential: LoginCredential | None = resolve_login_credential(shim)
    if credential is None or not credential.token:
        raise TokenLoginError(
            f"配置里没有可用令牌（{path}）：请填 [cli.login] token 或 token_file"
        )
    payload: dict[str, str] = {"token": credential.token}
    if credential.device_id:
        payload["device_id"] = credential.device_id
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出腾讯频道 CLI 的登录令牌（token 直登用）")
    parser.add_argument("output", nargs="?", default=DEFAULT_OUTPUT, help=f"输出 JSON 路径（默认 {DEFAULT_OUTPUT}）")
    parser.add_argument("--stdout", action="store_true", help="打印到标准输出而不写文件")
    parser.add_argument("--force", action="store_true", help="目标文件已存在时覆盖")
    parser.add_argument(
        "--from-config",
        nargs="?",
        const=DEFAULT_CONFIG,
        default="",
        metavar="PATH",
        help=f"从插件 config.toml 里读令牌（多实例机器必用；不传路径则用 {DEFAULT_CONFIG}）",
    )
    args = parser.parse_args(argv)

    try:
        if args.from_config:
            payload = export_payload_from_config(args.from_config)
            source = f"配置 {args.from_config}"
        else:
            payload = export_payload()
            source = "本机密钥链（全机器共享，多实例机器请改 --from-config）"
    except TokenLoginError as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    token = payload.get("token", "")
    print(f"已读取登录令牌（来源：{source}）：token={mask_secret(token)}，device_id={mask_secret(payload.get('device_id', ''))}")

    if args.stdout:
        print(text)
        return 0

    output = Path(args.output).expanduser()
    if output.exists() and not args.force:
        print(f"目标文件已存在（{output}）：加 --force 覆盖，或换一个路径", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text + "\n", encoding="utf-8")
    print(f"已写出：{output}")
    print("下一步：把路径填进 config.toml ——  ")
    print(f'  [cli.login]\n  token_file = "{output.as_posix()}"')
    print("⚠️ 该文件等同登录凭证，注意保管（不要进仓库/不要外发）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
