"""多实例插件源码同步 / 校验工具（维护脚本，**不是插件组件**）。

适用场景
--------
同一台机器（或同一份部署）上跑了**多个** Neo-MoFox 实例，各自从自己的
``plugins/tencent_channel`` 加载插件。两边源码必须**逐字节一致**，否则会出现
「同一个 bug 只在一侧修了」这类问题。

用法
----
::

    # 1) 只检查是否一致（退出码 0 = 一致，1 = 有差异；适合放进提交前检查）
    python sync_instances.py --source <A> --target <B> --check

    # 2) 把 A 那份同步到 B，然后自动复核
    python sync_instances.py --source <A> --target <B>

    # 3) 目标侧多出来的文件一并删掉（谨慎）
    python sync_instances.py --source <A> --target <B> --mirror

    # 4) 对调方向（等价于把 --source/--target 互换）
    python sync_instances.py --source <A> --target <B> --reverse

示例（Windows，注意路径用引号包住）::

    python sync_instances.py --source "D:\\bots\\instance-a\\plugins\\tencent_channel" ^
                             --target "D:\\bots\\instance-b\\plugins\\tencent_channel"

边界（重要）
------------
- ``--source`` / ``--target`` **必填**：本脚本不猜路径（随插件公开，不写死任何人的本机目录）。
- 只处理**插件目录**里的源码；``__pycache__`` 与 ``*.pyc`` 一律忽略（各实例自己编译）。
- 各实例的 ``config/plugins/tencent_channel/config.toml`` 与 ``login_token.json``
  **不在本脚本范围内** —— 那是各自的配置与凭证，绝不该同步。
- 本脚本自身也在插件目录里，所以两边永远是同一份；运行它不会造成自我漂移。

另一种彻底不漂移的做法（可选，需手工执行一次）：把其中一个目录换成目录联接（junction），
两边物理上就是同一份：:

    rmdir "<B>\\plugins\\tencent_channel"
    mklink /J "<B>\\plugins\\tencent_channel" "<A>\\plugins\\tencent_channel"

⚠️ 代价：之后用 ``Remove-Item -Recurse`` / ``rd /s`` 删该目录时**可能把联接指向的真实内容一起删掉**，
且源目录一旦移动/改名链接即断。所以默认建议用本脚本同步，而不是 junction。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

#: 不参与比对/同步的目录：编译产物、版本库、构建产物、工具缓存、虚拟环境
IGNORED_DIRS = {
    "__pycache__", ".git", "dist", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".venv", "venv", "env",
}
#: 不参与比对/同步的文件后缀（打包产物也同步过去没意义）
IGNORED_SUFFIXES = {".pyc", ".pyo", ".mfp"}


def tree(root: Path) -> dict[str, str]:
    """返回 ``相对路径 → SHA256``（忽略 ``IGNORED_DIRS`` 与 ``IGNORED_SUFFIXES``）。"""
    if not root.is_dir():
        raise SystemExit(f"目录不存在：{root}")
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if IGNORED_DIRS & set(path.parts) or path.suffix in IGNORED_SUFFIXES:
            continue
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def report(source: Path, target: Path, a: dict[str, str], b: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """打印差异，返回 ``(只在源, 只在目标, 内容不同)``。"""
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    changed = sorted(name for name in set(a) & set(b) if a[name] != b[name])
    print(f"源  : {source}（{len(a)} 个文件）")
    print(f"目标: {target}（{len(b)} 个文件）")
    if only_a or only_b or changed:
        print("存在差异：")
        if only_a:
            print(f"  只在源  : {', '.join(only_a)}")
        if only_b:
            print(f"  只在目标: {', '.join(only_b)}")
        if changed:
            print(f"  内容不同: {', '.join(changed)}")
    else:
        print("两份源码逐字节一致。")
    return only_a, only_b, changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步/校验 tencent_channel 插件源码（多实例）")
    parser.add_argument("--source", default="", help="源插件目录（必填）")
    parser.add_argument("--target", default="", help="目标插件目录（必填）")
    parser.add_argument("--reverse", action="store_true", help="对调 --source 与 --target")
    parser.add_argument("--check", action="store_true", help="只检查，不改动任何文件")
    parser.add_argument("--mirror", action="store_true", help="目标侧多出来的文件也删除（默认保留并报告）")
    args = parser.parse_args(argv)

    if not args.source or not args.target:
        parser.error(
            "必须用 --source 与 --target 指定两个插件目录，例如：\n"
            '  python sync_instances.py --source "D:\\a\\plugins\\tencent_channel" '
            '--target "D:\\b\\plugins\\tencent_channel"'
        )

    source = Path(args.target if args.reverse else args.source)
    target = Path(args.source if args.reverse else args.target)

    a, b = tree(source), tree(target)
    only_a, only_b, changed = report(source, target, a, b)

    if args.check:
        if only_a or only_b or changed:
            return 1
        return 0

    copied: list[str] = []
    deleted: list[str] = []
    for name in only_a + changed:
        src, dst = source / name, target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(name)
    if args.mirror:
        for name in only_b:
            (target / name).unlink()
            deleted.append(name)

    if copied:
        print(f"\n已复制 {len(copied)} 个文件：{', '.join(copied)}")
    if deleted:
        print(f"已删除目标侧多余文件 {len(deleted)} 个：{', '.join(deleted)}")
    if only_b and not args.mirror:
        print(f"\n目标侧多出 {len(only_b)} 个文件（默认保留；要清理请加 --mirror）：{', '.join(only_b)}")
    if not copied and not deleted:
        print("\n无需复制。")

    # 复核
    a2, b2 = tree(source), tree(target)
    still = [n for n in set(a2) if b2.get(n) != a2[n]]
    if still or (args.mirror and set(b2) - set(a2)):
        print("\n同步后仍有差异：")
        report(source, target, a2, b2)
        return 1
    print(f"\n同步完成：两份源码一致（{len(a2)} 个文件）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
