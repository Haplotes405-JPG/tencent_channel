"""从 ``tencent-channel-cli`` 的 schema 生成 ``capability_spec.py``。

用法（插件目录下）：

    python gen_capability_spec.py --schema <cli_schema_all.json> [--params <cli_command_params.json>]

- ``--schema``：``tencent-channel-cli schema -j`` 的输出（域 + 命令清单 + 分组 + 风险）
- ``--params``：可选，``<domain>.<command>`` → 参数定义的文件（由逐命令 ``schema`` 查询汇总），
  用于在能力清单里带上参数名与必填标记；没有就不带参数。

注意：本文件**不是**插件组件，只是维护脚本；升级 CLI 后重新跑一次即可。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).parent))
try:  # 复用 CLI 层的键名规范化（同目录、纯 stdlib，可独立导入）
    from cli_client import normalize_param_name  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - 生成器不该因为导入失败而不可用

    def normalize_param_name(name: str) -> str:  # type: ignore[misc]
        return str(name or "").strip().lstrip("-").replace("-", "_").lower()

#: 顶层命令（不属于 feed/manage 两个域），一并纳入能力清单
EXTRA_COMMANDS: tuple[dict[str, str], ...] = (
    {
        "name": "cli.version",
        "domain": "cli",
        "action": "version",
        "group": "read",
        "risk": "read",
        "summary": "查看 tencent-channel-cli 版本",
    },
    {
        "name": "cli.doctor",
        "domain": "cli",
        "action": "doctor",
        "group": "read",
        "risk": "read",
        "summary": "CLI 自检（版本/系统密钥链/登录状态/服务端点/业务探测）",
    },
)

HEADER = '''"""CLI 能力清单（**由 ``gen_capability_spec.py`` 生成，请勿手改**）。

- 生成时间：{generated_at}
- CLI 版本：{cli_version}
- 来源：``tencent-channel-cli schema -j``{params_note}

人工维护的部分（分类、开关判定、提示词）在 ``capabilities.py`` 里。
升级 CLI 后重新生成：``python gen_capability_spec.py --schema <schema.json> --params <params.json>``
"""

from __future__ import annotations

from typing import Any

SPEC_CLI_VERSION = "{cli_version}"
SPEC_GENERATED_AT = "{generated_at}"

CAPABILITIES: tuple[dict[str, Any], ...] = (
'''


def load_schema(path: pathlib.Path) -> tuple[list[dict[str, Any]], str]:
    """读取 ``schema -j`` 输出，返回 ``(能力列表, CLI 版本)``。"""
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    version = ""
    items: list[dict[str, Any]] = []
    domains = raw if isinstance(raw, list) else raw.get("domains", [])
    for domain_block in domains:
        domain = str(domain_block.get("domain", ""))
        for command in domain_block.get("commands", []):
            action = str(command.get("use", "")).strip()
            if not domain or not action:
                continue
            items.append(
                {
                    "name": f"{domain}.{action}",
                    "domain": domain,
                    "action": action,
                    "group": str(command.get("group", "")),
                    "risk": str(command.get("risk", "")),
                    "summary": str(command.get("short", "")),
                }
            )
    if isinstance(raw, dict):
        version = str(raw.get("version", "") or "")
    for extra in EXTRA_COMMANDS:
        items.append(dict(extra))
    return items, version


def load_params(path: pathlib.Path | None) -> dict[str, dict[str, list[str]]]:
    """读取逐命令参数文件，返回 ``{能力名: {"params": [...], "required": [...]}}``。

    参数名统一规范成 **stdin JSON 键名**（kebab→snake，``image``→``file_paths`` 等），
    因为插件调用 CLI 走的是 stdin JSON 而不是 flag。
    """
    if path is None or not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    commands = raw.get("commands", raw)
    result: dict[str, dict[str, list[str]]] = {}
    if not isinstance(commands, dict):
        return result
    for name, entry in commands.items():
        if not isinstance(entry, dict):
            continue
        raw_params = entry.get("params") or entry.get("parameters") or []
        names: list[str] = []
        required: list[str] = []
        if isinstance(raw_params, dict):
            raw_params = [
                {"name": key, **(value if isinstance(value, dict) else {})}
                for key, value in raw_params.items()
            ]
        for param in raw_params if isinstance(raw_params, list) else []:
            if not isinstance(param, dict):
                continue
            raw_name = str(param.get("name") or param.get("flag") or "").strip()
            if not raw_name:
                continue
            pname = normalize_param_name(raw_name)
            if not pname or pname in ("help", "json", "yes", "dry_run", "log_level", "verbose"):
                continue
            if pname not in names:
                names.append(pname)
            if bool(param.get("required")) and pname not in required:
                required.append(pname)
        result[str(name)] = {"params": names, "required": required}
    return result


def load_version(schema_path: pathlib.Path, params_path: pathlib.Path | None) -> str:
    """尽力取 CLI 版本：先看参数汇总文件，再看 schema 文件。"""
    for path in (params_path, schema_path):
        if path is None or not path.exists():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(raw, dict):
            version = str(raw.get("cli_version", "") or "")
            if version:
                return version
    return "unknown"


def render(items: list[dict[str, Any]], params: dict[str, dict[str, list[str]]], version: str,
           generated_at: str, params_note: str) -> str:
    """渲染 ``capability_spec.py``。"""
    lines = [HEADER.format(generated_at=generated_at, cli_version=version, params_note=params_note)]
    for item in items:
        details = params.get(item["name"], {})
        param_names = list(details.get("params", []))
        required = list(details.get("required", []))
        block = (
            "    {{\n"
            '        "name": "{name}",\n'
            '        "domain": "{domain}",\n'
            '        "action": "{action}",\n'
            '        "group": "{group}",\n'
            '        "risk": "{risk}",\n'
            '        "summary": "{summary}",\n'
            '        "params": {params},\n'
            '        "required": {required},\n'
            "    }},"
        ).format(
            name=item["name"],
            domain=item["domain"],
            action=item["action"],
            group=item["group"],
            risk=item["risk"],
            summary=item["summary"].replace('"', "'"),
            params=json.dumps(param_names, ensure_ascii=False),
            required=json.dumps(required, ensure_ascii=False),
        )
        lines.append(block)
    lines.append(")\n")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 capability_spec.py")
    parser.add_argument("--schema", required=True, help="tencent-channel-cli schema -j 的输出文件")
    parser.add_argument("--params", default="", help="逐命令参数汇总文件（可选）")
    parser.add_argument(
        "--out",
        default=str(pathlib.Path(__file__).with_name("capability_spec.py")),
        help="输出路径（默认写到本脚本同目录）",
    )
    args = parser.parse_args()

    schema_path = pathlib.Path(args.schema)
    params_path = pathlib.Path(args.params) if args.params else None
    items, version = load_schema(schema_path)
    params = load_params(params_path)
    if not version or version == "unknown":
        version = load_version(schema_path, params_path)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    note = " + 逐命令 schema" if params else "（未提供参数文件，能力清单不带参数名）"
    text = render(items, params, version or "unknown", generated_at, note)
    pathlib.Path(args.out).write_text(text, encoding="utf-8")
    print(f"已生成 {args.out}：能力 {len(items)} 条，带参数 {len(params)} 条，CLI 版本 {version or 'unknown'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
