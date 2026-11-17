#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""包结构测试：模块边界、依赖方向、单文件构建产物一致性。

与 test_core.py 互补：那个测「行为对不对」，这个测「工程结构对不对」。
"""

import ast
import importlib
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# src 布局：包在 src/ 下，测试直接从源码树导入（等价于 pip install -e .）
sys.path.insert(0, str(ROOT / "src"))

import ugreen  # noqa: E402

PKG = ROOT / "src" / "ugreen"
SINGLE = ROOT / "ugreen_ai_reply.py"

# 子包 -> 该子包下的模块
SUBPACKAGES = {
    "core": ["constants", "models", "config", "deps"],
    "util": ["envutil", "textutil", "timeutil", "aiutil"],
    "forum": ["client", "classify", "state", "revisit"],
    "search": ["providers"],
    "ai": ["client", "quality"],
    "runtime": ["cli", "notify", "report"],
}

# 依赖方向：左边不得 import 右边（避免循环依赖与分层倒置）
LAYER_ORDER = [
    "core.deps",
    "core.constants",
    "util.envutil",
    "util.textutil",
    "util.timeutil",
    "util.aiutil",
    "core.models",
    "forum.classify",
    "core.config",
    "forum.client",
    "search.providers",
    "ai.quality",
    "ai.client",
    "forum.revisit",
    "forum.state",
    "runtime.notify",
    "runtime.report",
    "runtime.cli",
]


def module_files() -> list[Path]:
    return sorted(p for p in PKG.rglob("*.py")
                  if p.name != "__init__.py")


def module_id(path: Path) -> str:
    """src/ugreen/forum/client.py -> forum.client"""
    rel = path.relative_to(PKG).with_suffix("")
    return ".".join(rel.parts)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def import_targets(path: Path) -> set[str]:
    """该模块导入的兄弟模块 id（包内相对导入解析成点分路径）。"""
    out: set[str] = set()
    pkg_parts = path.relative_to(PKG).parts[:-1]
    for node in ast.walk(parse(path)):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        if node.level == 0:
            continue  # 绝对导入（标准库/三方）
        base = list(pkg_parts)
        # level=1 表示当前包，level=2 表示上一级
        if node.level > 1:
            base = base[:len(base) - (node.level - 1)]
        if node.module:
            base = base + node.module.split(".")
        else:
            base = base + [a.name for a in node.names][:1]
        # from . import x  -> x 是子模块
        if not node.module:
            for a in node.names:
                out.add(".".join(base + [a.name]))
            continue
        out.add(".".join(base))
    return out


class PackageLayoutTest(unittest.TestCase):
    """包结构基本形状。"""

    def test_subpackages_exist(self):
        for pkg, mods in SUBPACKAGES.items():
            self.assertTrue((PKG / pkg / "__init__.py").exists(),
                            f"缺少子包 {pkg}/__init__.py")
            for mod in mods:
                self.assertTrue((PKG / pkg / f"{mod}.py").exists(),
                                f"缺少模块 {pkg}/{mod}.py")

    def test_has_init_and_main(self):
        self.assertTrue((PKG / "__init__.py").exists())
        self.assertTrue((PKG / "__main__.py").exists())

    def test_no_duplicate_top_level_names(self):
        """模块之间不能有同名顶层符号，否则内联成单文件会互相覆盖。"""
        seen: dict[str, str] = {}
        dupes = []
        for p in module_files():
            for node in parse(p).body:
                names = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    names = [node.name]
                elif isinstance(node, ast.Assign):
                    names = [t.id for t in node.targets
                             if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(
                        node.target, ast.Name):
                    names = [node.target.id]
                for n in names:
                    if n in seen and seen[n] != module_id(p):
                        dupes.append(f"{n}（{seen[n]} 与 {module_id(p)}）")
                    seen[n] = module_id(p)
        self.assertEqual(dupes, [], f"跨模块重名：{dupes}")

    def test_dependency_direction(self):
        """上层可以依赖下层，下层不得依赖上层。"""
        idx = {name: i for i, name in enumerate(LAYER_ORDER)}
        violations = []
        for p in module_files():
            mid = module_id(p)
            if mid not in idx:
                continue
            for dep in import_targets(p):
                if dep in idx and idx[dep] > idx[mid]:
                    violations.append(f"{mid} -> {dep}")
        self.assertEqual(violations, [],
                         f"存在反向依赖（会形成循环）：{violations}")

    def test_no_module_imports_package_root(self):
        """子模块不能 from . import xxx，否则会触发循环导入。"""
        bad = []
        for p in module_files():
            for node in ast.walk(parse(p)):
                if (isinstance(node, ast.ImportFrom) and node.level == 1
                        and node.module is None):
                    bad.append(module_id(p))
        self.assertEqual(bad, [], "子模块不应从包根导入")

    def test_package_exports_public_api(self):
        """包根导出的名字都应真实存在。"""
        for name in ugreen.__all__:
            self.assertTrue(hasattr(ugreen, name),
                            f"__all__ 里的 {name} 不存在")


class SingleFileBuildTest(unittest.TestCase):
    """单文件产物必须与包保持同步。"""

    def test_single_file_exists(self):
        self.assertTrue(SINGLE.exists(), "缺少单文件产物")

    def test_single_file_is_up_to_date(self):
        """改了 src/ugreen/ 却忘了重新构建 —— 这是最容易被漏掉的坑。"""
        r = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_single.py"), "--check"],
            capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(r.returncode, 0,
                         f"单文件已过期，请运行 python tools/build_single.py\n"
                         f"{r.stdout}{r.stderr}")

    def test_single_file_importable(self):
        mod = importlib.import_module("ugreen_ai_reply")
        self.assertTrue(callable(mod.main))
        self.assertTrue(hasattr(mod, "Thread"))
        self.assertTrue(hasattr(mod, "Config"))

    def test_single_and_package_behave_same(self):
        """同一输入下，单文件与包的关键函数输出必须一致。"""
        single = importlib.import_module("ugreen_ai_reply")
        cases = [
            ("parse_guide_time", ("2026-11-17 14:30",), {}),
            ("tid_from_url", ("forum.php?mod=viewthread&tid=7342",), {}),
            ("clean_text", ("  A&nbsp;<b>B</b>  ",), {}),
            ("extract_title_token", ("【自荐】我的NAS工具",), {}),
        ]
        for name, args, kwargs in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(single, name)(*args, **kwargs),
                    getattr(ugreen, name)(*args, **kwargs))

    def test_single_file_has_no_relative_imports(self):
        """单文件不能残留相对导入，否则脱离目录就崩。"""
        src = SINGLE.read_text(encoding="utf-8")
        self.assertNotIn("from .", src)
        self.assertNotIn("from ..", src)
        self.assertNotIn("import ugreen.", src)


class ModuleResponsibilityTest(unittest.TestCase):
    """各模块职责边界。"""

    def test_constants_holds_prompt(self):
        from ugreen.core import constants
        self.assertIn("绿联", constants.SYSTEM_PROMPT)
        self.assertEqual(constants.DEFAULT_FORUMS[10], "功能讨论")

    def test_state_holds_quota(self):
        from ugreen.forum import state
        self.assertEqual(state.DAILY_KEEP_DAYS, 30)

    def test_deps_exposes_parser(self):
        from ugreen.core import deps
        self.assertIn(deps.HTML_PARSER, ("lxml", "html.parser"))

    def test_util_package_splits_by_concern(self):
        """util 按职责拆成 env / text / timeutil / aiutil。"""
        from ugreen.util import aiutil, envutil, textutil, timeutil
        self.assertTrue(callable(envutil.log))
        self.assertTrue(callable(textutil.clean_text))
        self.assertTrue(callable(timeutil.parse_rss_date))
        self.assertTrue(callable(aiutil.normalize_ai_base))

    def test_config_reads_env(self):
        import os
        from unittest.mock import patch
        from ugreen.core import Config
        with patch.dict(os.environ, {"UGREEN_DAILY_QUOTA": "7"}, clear=True):
            cfg = Config.from_env(ugreen.build_parser().parse_args([]))
        self.assertEqual(cfg.daily_quota, 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
