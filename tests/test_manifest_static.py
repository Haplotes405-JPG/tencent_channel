"""静态检查：manifest 与代码/文档的一致性（纯 AST + 文本，不导入插件）。

对应插件市场 ``mpdt plugin check`` 里能被静态判定的部分，便于提交前自测。

运行（在插件目录的**上一级**执行）：

    python -m unittest discover -s tencent_channel/tests -t . -p "test_*.py"
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
ALLOWED_CATEGORIES = {"tool", "chat", "fun", "information", "moderation"}
ALLOWED_COMPONENT_TYPES = {
    "action", "tool", "adapter", "chatter", "command",
    "event_handler", "service", "router", "config", "agent",
}
LEGAL_API_MODULES = {
    "action_api", "adapter_api", "agent_api", "chat_api", "command_api", "config_api",
    "database_api", "event_api", "llm_api", "log_api", "media_api", "message_api",
    "permission_api", "plugin_api", "prompt_api", "router_api", "send_api",
    "service_api", "storage_api", "stream_api",
}


def load_manifest() -> dict:
    return json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))


def declared_component_names() -> set[str]:
    """AST 扫描代码里声明的组件名（显式属性 + 类名推导）。"""

    def snake(text: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", text).lower()

    names: set[str] = set()
    for path in PLUGIN_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            attrs = {
                stmt.targets[0].id: stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            }
            for attr in ("tool_name", "action_name", "chatter_name", "command_name",
                         "service_name", "router_name", "handler_name", "agent_name",
                         "adapter_name", "name"):
                if attr in attrs:
                    names.add(attrs[attr])
            bases = {ast.unparse(b) for b in node.bases}
            if any("Config" in b or "SectionBase" in b for b in bases):
                names.update({attrs.get("config_name", "config"), attrs.get("name", "config")})
            elif any(b.endswith(("Adapter", "EventHandler")) for b in bases):
                base = snake(node.name)
                names.add(base)
                for suffix in ("_handler", "_adapter"):
                    if base.endswith(suffix):
                        names.add(base[: -len(suffix)])
    return names


class ManifestTest(unittest.TestCase):
    """manifest.json 必填字段与取值约束。"""

    def setUp(self) -> None:
        self.manifest = load_manifest()

    def test_required_fields(self) -> None:
        for field in ("name", "version", "description", "author", "dependencies", "entry_point"):
            self.assertIn(field, self.manifest, f"缺少必填字段 {field}")
            self.assertTrue(self.manifest[field], f"字段 {field} 为空")

    def test_semver(self) -> None:
        self.assertRegex(str(self.manifest["version"]), r"^\d+\.\d+\.\d+")

    def test_categories_single_and_allowed(self) -> None:
        categories = self.manifest.get("categories") or []
        self.assertEqual(len(categories), 1, "categories 只允许一个值")
        self.assertIn(categories[0], ALLOWED_CATEGORIES)

    def test_tags(self) -> None:
        tags = self.manifest.get("tags") or []
        self.assertTrue(tags)
        self.assertTrue(all(isinstance(tag, str) and tag for tag in tags))

    def test_api_version_declared_and_legal(self) -> None:
        api_version = self.manifest.get("api_version")
        self.assertIsInstance(api_version, dict, "建议用 dict 形式精确声明用到的 API 模块")
        for module, version in api_version.items():
            self.assertIn(module, LEGAL_API_MODULES, f"非法 API 模块名 {module}")
            self.assertRegex(str(version), r"^\d+\.\d+\.\d+$")

    def test_declared_api_modules_match_imports(self) -> None:
        imported: set[str] = set()
        for path in PLUGIN_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            imported |= set(re.findall(r"plugin_system\.api\.(\w+)", text))
            for match in re.findall(r"plugin_system\.api import ([^\n#]+)", text):
                imported |= {name.strip() for name in match.split(",") if name.strip()}
        imported &= LEGAL_API_MODULES
        declared = set(self.manifest.get("api_version") or {})
        self.assertEqual(imported - declared, set(), "代码里用到但未声明的 API 模块")

    def test_component_types_allowed(self) -> None:
        for item in self.manifest.get("include") or []:
            self.assertIn(item["component_type"], ALLOWED_COMPONENT_TYPES)

    def test_include_names_exist_in_code(self) -> None:
        declared = declared_component_names()
        missing = [i["component_name"] for i in self.manifest.get("include") or []
                   if i["component_name"] not in declared]
        self.assertEqual(missing, [], f"include 里这些组件在代码里找不到：{missing}")

    def test_entry_point_and_plugin_class_consistent(self) -> None:
        entry = PLUGIN_DIR / self.manifest["entry_point"]
        self.assertTrue(entry.is_file())
        text = entry.read_text(encoding="utf-8")
        self.assertIn(f'plugin_name = "{self.manifest["name"]}"', text)
        self.assertIn(f'plugin_version = "{self.manifest["version"]}"', text)


class StructureTest(unittest.TestCase):
    """目录卫生与许可/文档完整性。"""

    #: 工具链/解释器生成的可再生缓存（跑 mpdt check / ruff / mypy 后必然出现）
    TRANSIENT = ("__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache")

    def test_no_build_artifacts(self) -> None:
        """源码树里不应留有字节码与工具缓存（打包前自查）。

        这些目录都是可再生的（跑测试 / mpdt check 后必然重新出现），所以这里**先清理再断言**：
        这样测试既能当打包前的门禁，又不会因为上一步跑过工具而误报。
        """
        removed: list[str] = []
        for path in sorted(PLUGIN_DIR.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.name in self.TRANSIENT and path.is_dir():
                removed.append(str(path.relative_to(PLUGIN_DIR)))
                shutil.rmtree(path, ignore_errors=True)
        if removed:
            print(f"（已清理可再生缓存 {len(removed)} 个：{', '.join(sorted(removed))}）")

        junk = [p for p in PLUGIN_DIR.rglob("*")
                if "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo")
                or p.name in self.TRANSIENT]
        self.assertEqual([str(p.relative_to(PLUGIN_DIR)) for p in junk], [])

    def test_readme_and_license(self) -> None:
        readme = PLUGIN_DIR / "README.md"
        self.assertTrue(readme.is_file() and readme.stat().st_size > 2000)
        license_file = PLUGIN_DIR / "LICENSE"
        self.assertTrue(license_file.is_file())
        head = [line.strip() for line in license_file.read_text(encoding="utf-8").splitlines() if line.strip()][:2]
        manifest_license = load_manifest().get("license")
        expected = {
            "GPL-3.0": "GNU GENERAL PUBLIC LICENSE",
            "AGPL-3.0": "GNU AFFERO GENERAL PUBLIC LICENSE",
            "MIT": "MIT License",
            "Apache-2.0": "Apache License",
        }.get(str(manifest_license))
        if expected:
            self.assertIn(expected, head, f"LICENSE 与 manifest.license={manifest_license} 不匹配")

    def test_no_local_paths_or_secrets(self) -> None:
        """示例与注释里不得出现本机路径、凭据，或**真实**的频道/用户/版块标识。

        这条是回归门禁：投稿/发布前必须保持「示例只用占位值」。
        """
        patterns = (
            # 本机路径 / 凭据
            r"[A-Za-z]:\\[^\s\"']*新建文件夹",
            r"C:\\Users\\\d{4,}",
            r"ghp_[A-Za-z0-9]{10,}",
            r"p_skey",
            # 真实频道 / 版块（新旧频道的 ID、频道号、版块 ID 与名称）
            r"34503051784260243",
            r"595841934087025973",
            r"pd65326351",
            r"pd73978018",
            r"736897699",
            r"743585677",
            r"743585722",
            r"743799212",
            r"奥赫玛",
            r"神悟树庭",
            r"雅努萨波利",
            r"永恒之地",
            # 真实账号 / 成员标识
            r"144115221053180883",
            r"144115221182595360",
            r"144115220540281238",
            r"HapLotes405",
            r"昔涟",
            r"小小涟",
            r"某不玩矢量",
            # 真实帖子/评论 ID 前缀（形如 B_018fc56a…）
            r"\bB_[0-9a-fA-F]{8,}",
            r"\bc_[0-9a-fA-F]{8,}",
            r"\br_[0-9a-fA-F]{8,}",
        )
        hits: list[str] = []
        targets = [*PLUGIN_DIR.glob("*.py"), *PLUGIN_DIR.rglob("*.md"), PLUGIN_DIR / "manifest.json"]
        for path in targets:
            body = path.read_text(encoding="utf-8", errors="replace")
            hits += [
                f"{path.relative_to(PLUGIN_DIR)}:{pattern}"
                for pattern in patterns
                if re.search(pattern, body)
            ]
        self.assertEqual(hits, [], f"发现本机路径、凭据或真实标识：{hits}")


if __name__ == "__main__":
    unittest.main()
