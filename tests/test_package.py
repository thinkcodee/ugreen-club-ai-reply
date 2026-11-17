#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""包结构测试：模块边界、依赖方向、单文件构建产物一致性。

与 test_core.py 互补：那个测「行为对不对」，这个测「工程结构对不对」。
"""

import ast
import importlib
import re
import subprocess
import sys
import unittest
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ImportError:  # pragma: no cover - Python 3.9 / 3.10
    tomllib = None


def load_toml(path: Path) -> dict:
    """读 pyproject.toml。3.11+ 用标准库 tomllib，低版本用最小正则解析。

    只解析这个测试需要的字段（dependencies 数组 / tool.setuptools 段 /
    project.scripts / project.name），不引第三方 tomli 依赖 —— CI 要覆盖 3.9。
    """
    if tomllib is not None:
        return tomllib.loads(path.read_text(encoding="utf-8"))

    src = path.read_text(encoding="utf-8")
    out: dict = {"project": {}, "tool": {"setuptools": {}}}

    m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", src, re.S | re.M)
    if m:
        out["project"]["dependencies"] = re.findall(
            r'"([^"]+)"', m.group(1))

    m = re.search(r'package-dir\s*=\s*\{\s*""\s*=\s*"([^"]+)"\s*\}', src)
    if m:
        out["tool"]["setuptools"]["package-dir"] = {"": m.group(1)}

    m = re.search(r"where\s*=\s*\[([^\]]*)\]", src)
    if m:
        out["tool"]["setuptools"].setdefault("packages", {})["find"] = {
            "where": re.findall(r'"([^"]+)"', m.group(1))}

    # project.name
    m = re.search(r'^name\s*=\s*"([^"]+)"', src, re.M)
    if m:
        out["project"]["name"] = m.group(1)

    # project.version
    m = re.search(r'^version\s*=\s*"([^"]+)"', src, re.M)
    if m:
        out["project"]["version"] = m.group(1)

    # license = { file = "LICENSE" }  或  license = { text = "MIT" }
    m = re.search(r"^license\s*=\s*\{(.*?)\}", src, re.S | re.M)
    if m:
        body = m.group(1)
        mf = re.search(r'file\s*=\s*"([^"]+)"', body)
        mt = re.search(r'text\s*=\s*"([^"]+)"', body)
        if mf:
            out["project"]["license"] = {"file": mf.group(1)}
        elif mt:
            out["project"]["license"] = {"text": mt.group(1)}

    # [project.scripts] 段：命令名 = "模块:函数"
    m = re.search(r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)", src,
                  re.S | re.M)
    if m:
        out["project"]["scripts"] = {
            k: v for k, v in re.findall(
                r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"([^"]+)"', m.group(1), re.M)}

    return out

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

    def test_single_file_has_no_excess_blank_lines(self):
        """单文件里不许出现 3 行以上连续空行。

        历史坑：构建器跳过 import 行时没连带清掉周围的空行，
        导致模块段落之间堆出 5~7 行空白。此测试锁死该行为，
        防止将来改构建器时复发。
        """
        lines = SINGLE.read_text(encoding="utf-8").splitlines()
        run = 0
        bad = []
        for i, ln in enumerate(lines, 1):
            if ln.strip() == "":
                run += 1
                if run > 2:
                    bad.append(i)
            else:
                run = 0
        self.assertEqual(bad, [],
                         f"单文件存在 3+ 连续空行，起始行：{bad[:10]}")

    def test_single_file_module_blocks_are_neat(self):
        """每个模块段落标记后，注释与首个定义之间固定 2 个空行。"""
        lines = SINGLE.read_text(encoding="utf-8").splitlines()
        problems = []
        for i, ln in enumerate(lines):
            if not ln.startswith("# ===== "):
                continue
            # 跳过段落标记与其后的说明注释
            j = i + 1
            while j < len(lines) and lines[j].startswith("#"):
                j += 1
            blanks = 0
            while j < len(lines) and lines[j].strip() == "":
                blanks += 1
                j += 1
            if blanks != 2:
                problems.append(f"{ln.strip()} 后有 {blanks} 个空行")
        self.assertEqual(problems, [],
                         f"模块段落间距不统一：{problems}")

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


class DependencyDeclarationTest(unittest.TestCase):
    """requirements.txt 与 pyproject.toml 的依赖必须一致。

    两处是手工维护的（依赖只有一个模块，不太值得上自动生成），
    所以用测试守住，防止以后加了依赖只改一处。
    """

    def _requirements(self) -> dict[str, str]:
        out = {}
        for line in (ROOT / "requirements.txt").read_text(
                encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name = re.split(r"[><=!~\[]", line)[0].strip()
            out[name] = line
        return out

    def _pyproject(self) -> dict[str, str]:
        data = load_toml(ROOT / "pyproject.toml")
        out = {}
        for spec in data["project"]["dependencies"]:
            name = re.split(r"[><=!~\[]", spec)[0].strip()
            out[name] = spec
        return out

    def test_requirements_and_pyproject_agree(self):
        req = self._requirements()
        py = self._pyproject()
        self.assertEqual(set(req), set(py),
                         f"依赖不一致：requirements.txt={sorted(req)} "
                         f"pyproject.toml={sorted(py)}")

    def test_version_specs_agree(self):
        """版本约束也要一致，不能一个 >=2.28 另一个 >=2.25。"""
        req = self._requirements()
        py = self._pyproject()
        for name, spec in req.items():
            self.assertEqual(spec, py.get(name),
                             f"{name} 的版本约束不一致："
                             f"{spec} vs {py.get(name)}")

    def test_pyproject_declares_src_layout(self):
        """src 布局必须在 pyproject 里声明，否则装出来的包是空的。"""
        data = load_toml(ROOT / "pyproject.toml")
        self.assertEqual(data["tool"]["setuptools"]["package-dir"], {"": "src"})
        self.assertEqual(data["tool"]["setuptools"]["packages"]["find"]["where"],
                         ["src"])

    def test_entry_points_resolve(self):
        """entry point 必须指向真实存在的模块。

        历史坑：cli.py 挪进 runtime/ 子包后，pyproject 里的
        `ugreen.cli:main` 没跟着改，装完命令一跑就
        ModuleNotFoundError: No module named 'ugreen.cli'。
        """
        data = load_toml(ROOT / "pyproject.toml")
        scripts = data.get("project", {}).get("scripts", {})
        self.assertTrue(scripts, "pyproject.toml 未声明任何命令行入口")
        for cmd, target in scripts.items():
            mod_path, _, func = target.partition(":")
            rel = Path(*mod_path.split(".")).with_suffix(".py")
            self.assertTrue((ROOT / "src" / rel).exists(),
                            f"命令 {cmd} 指向的模块不存在：{mod_path} "
                            f"(期望 {ROOT / 'src' / rel})")
            # 目标函数也要真的在这个模块里
            tree = parse(ROOT / "src" / rel)
            funcs = {n.name for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            self.assertIn(func, funcs,
                          f"命令 {cmd} 指向的函数 {func} 不在 {mod_path} 里")

    def test_project_name_matches_repo(self):
        """pyproject 里的包名应与仓库名一致。"""
        data = load_toml(ROOT / "pyproject.toml")
        self.assertEqual(data["project"]["name"], "ugreen-club-ai-reply")
        # 命令行入口名字通常跟包名一致
        for cmd in data.get("project", {}).get("scripts", {}):
            self.assertTrue(cmd.startswith("ugreen-club-ai-reply"),
                            f"命令行入口 {cmd} 与包名不一致")

    def test_version_consistent_everywhere(self):
        """版本号必须在 pyproject、包、单文件三处一致。

        历史坑：v1.2.0 发布时三处版本号还停在 1.1.6，
        发出去的包元数据与实际功能对不上。
        """
        data = load_toml(ROOT / "pyproject.toml")
        py_ver = data["project"]["version"]

        pkg_ver = ugreen.__version__

        single_src = SINGLE.read_text(encoding="utf-8")
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', single_src, re.M)
        self.assertIsNotNone(m, "单文件里找不到 __version__ 定义")
        single_ver = m.group(1)

        self.assertEqual(py_ver, pkg_ver,
                         f"pyproject 版本 {py_ver} 与包内 __version__ {pkg_ver} 不一致")
        self.assertEqual(py_ver, single_ver,
                         f"pyproject 版本 {py_ver} 与单文件 {single_ver} 不一致")

    def test_version_matches_latest_tag(self):
        """版本号应与最新的 git tag 对应。

        只在「最新 tag 正好指向当前提交」时检查 ——
        否则开发中的中间提交本来就不该等于某个 tag。
        """
        try:
            out = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                capture_output=True, text=True, cwd=str(ROOT), timeout=30)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, cwd=str(ROOT), timeout=30)
            tag_sha = subprocess.run(
                ["git", "rev-list", "-n", "1", out.stdout.strip()],
                capture_output=True, text=True, cwd=str(ROOT), timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("无法调用 git")
        if out.returncode != 0 or not out.stdout.strip():
            self.skipTest("本地没有可用的 git tag")
        if tag_sha.stdout.strip() != head.stdout.strip():
            # 最新 tag 指向的是别的提交（开发中的中间状态），跳过
            self.skipTest("最新 tag 未指向当前提交")

        latest = out.stdout.strip()
        data = load_toml(ROOT / "pyproject.toml")
        self.assertEqual(data["project"]["version"], latest.lstrip("v"),
                         f"版本号 {data['project']['version']} 与最新 tag {latest} 不一致")


class LicenseTest(unittest.TestCase):
    """许可证文件必须存在、合法、且与各处声明一致。"""

    def setUp(self):
        self.license = ROOT / "LICENSE"
        self.text = (self.license.read_text(encoding="utf-8")
                     if self.license.exists() else "")

    def test_license_file_exists(self):
        self.assertTrue(self.license.exists(),
                        "仓库根目录缺少 LICENSE 文件")
        self.assertGreater(len(self.text), 1000,
                           "LICENSE 内容异常短，可能不是完整许可证全文")

    def test_license_is_mit(self):
        """必须是标准 MIT 全文（关键条款都在）。"""
        self.assertIn("MIT License", self.text)
        for clause in (
            "Permission is hereby granted, free of charge",
            "without restriction",
            "The above copyright notice and this permission notice",
            "THE SOFTWARE IS PROVIDED \"AS IS\"",
            "WITHOUT WARRANTY OF ANY KIND",
        ):
            self.assertIn(clause, self.text, f"LICENSE 缺少 MIT 条款：{clause}")

    def test_license_has_copyright_holder(self):
        self.assertRegex(self.text, r"Copyright \(c\) \d{4}.+\S",
                         "LICENSE 缺少版权人与年份")

    def test_pyproject_license_matches_file(self):
        """pyproject 声明的 license 必须指向这个文件，且该文件是 MIT。"""
        data = load_toml(ROOT / "pyproject.toml")
        lic = data["project"].get("license")
        self.assertIsNotNone(lic, "pyproject.toml 未声明 license")

        # license = { file = "LICENSE" } 或 license = { text = "MIT" }
        if isinstance(lic, dict) and "file" in lic:
            path = ROOT / lic["file"]
            self.assertTrue(path.exists(),
                            f"pyproject 指向的许可证文件不存在：{path}")
            self.assertIn("MIT License", path.read_text(encoding="utf-8"),
                          f"{path} 不是 MIT 许可证")
        else:
            self.assertIn("MIT", str(lic),
                          f"pyproject 的 license 声明不是 MIT：{lic}")

    def test_single_file_carries_license(self):
        """单文件是用户实际拿到手的产物，必须自带许可证声明。

        青龙订阅只下载这一个 .py，没有 LICENSE 跟着走，
        MIT 要求的「保留版权声明」就靠单文件末尾那块注释。
        """
        src = SINGLE.read_text(encoding="utf-8")
        self.assertIn("MIT License", src,
                      "单文件里没有许可证声明")
        self.assertIn("Copyright (c)", src,
                      "单文件里没有版权声明")
        # 关键：许可证块必须在文件末尾（__main__ 入口之后）
        self.assertGreater(src.rindex("MIT License"),
                           src.rindex('if __name__ == "__main__"'),
                           "单文件的许可证块应放在文件末尾")

    def test_readme_has_license_section(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("MIT", readme, "README 没有提到 MIT 许可证")
        self.assertIn("LICENSE", readme, "README 没有链接到 LICENSE 文件")


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
