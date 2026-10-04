from __future__ import annotations

import ast
from pathlib import Path
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "runtime"
ASSISTANT_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "assistant"
LLM_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "llm"
MCP_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "mcp"
SKILLS_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "skills"
CLI_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "cli"
TOOLS_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "tools"
SANDBOX_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "sandbox"
CHANNELS_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "channels"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "redpanda" / "config.py"
BOOTSTRAP_PATH = Path(__file__).resolve().parents[2] / "redpanda" / "bootstrap.py"
THINLLM_ROOT = Path(__file__).resolve().parents[2] / "thinllm"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _imports_any(modules: set[str], prefixes: set[str]) -> set[str]:
    return {
        module
        for module in modules
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes)
    }


class LayerImportBoundaryTest(unittest.TestCase):
    def test_skills_do_not_import_runtime_or_assistant(self):
        offenders: list[str] = []
        for path in sorted(SKILLS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                _imports_any(
                    modules,
                    {"redpanda.assistant", "redpanda.runtime"},
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(SKILLS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_cli_does_not_import_runtime_or_product_layers(self):
        # cli 声明的对外依赖只有 sandbox（进程执行能力）与 tools（ToolSpec 契约）。
        offenders: list[str] = []
        forbidden = {
            "redpanda.assistant",
            "redpanda.channels",
            "redpanda.llm",
            "redpanda.mcp",
            "redpanda.runtime",
            "redpanda.skills",
        }
        for path in sorted(CLI_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(_imports_any(modules, forbidden))
            if leaked:
                offenders.append(
                    f"{path.relative_to(CLI_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_mcp_does_not_import_runtime_or_assistant(self):
        offenders: list[str] = []
        for path in sorted(MCP_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                _imports_any(
                    modules,
                    {"redpanda.assistant", "redpanda.runtime"},
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(MCP_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_mcp_uses_namespaced_package_without_shadowing_sdk(self):
        self.assertTrue(MCP_ROOT.is_dir())
        self.assertFalse((MCP_ROOT.parents[1] / "mcp").exists())

    def test_llm_does_not_import_product_or_execution_layers(self):
        offenders: list[str] = []
        for path in sorted(LLM_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                _imports_any(
                    modules,
                    {
                        "redpanda.assistant",
                        "redpanda.channels",
                        "redpanda.cli",
                        "redpanda.mcp",
                        "redpanda.runtime",
                        "redpanda.skills",
                        "redpanda.tools",
                    },
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(LLM_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_assistant_uses_only_the_llm_api_port(self):
        offenders: list[str] = []
        for path in sorted(ASSISTANT_ROOT.rglob("*.py")):
            unexpected = sorted(
                module
                for module in _imported_modules(path)
                if module.startswith("redpanda.llm.")
                and module != "redpanda.llm.api"
            )
            if unexpected:
                offenders.append(
                    f"{path.relative_to(ASSISTANT_ROOT)}: {', '.join(unexpected)}"
                )
        self.assertEqual(offenders, [])

    def test_bootstrap_not_config_owns_the_concrete_llm_client(self):
        self.assertNotIn("thinllm", _imported_modules(CONFIG_PATH))
        self.assertIn("thinllm", _imported_modules(BOOTSTRAP_PATH))

    def test_thinllm_does_not_import_redpanda(self):
        offenders: list[str] = []
        for path in sorted(THINLLM_ROOT.rglob("*.py")):
            leaked = sorted(_imports_any(_imported_modules(path), {"redpanda"}))
            if leaked:
                offenders.append(
                    f"{path.relative_to(THINLLM_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_worker_config_does_not_construct_the_concrete_llm_client(self):
        import inspect

        from redpanda.bootstrap import worker_config

        self.assertNotIn("ChatCompletionsClient", inspect.getsource(worker_config))

    def test_assistant_does_not_import_foreign_llm_clients(self):
        offenders: list[str] = []
        for path in sorted(ASSISTANT_ROOT.rglob("*.py")):
            leaked = sorted(
                module
                for module in _imported_modules(path)
                if module == "httpx" or module.startswith("httpx.")
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(ASSISTANT_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_channels_do_not_import_runtime_or_infrastructure_layers(self):
        offenders: list[str] = []
        forbidden = {
            "redpanda.llm",
            "redpanda.runtime",
            "redpanda.tools",
        }
        # sandbox 对 channels 只开放 registry：WorkspaceRecord / WorkspaceRegistry
        # 是各层共享的工作区元数据定义，不含进程执行能力；执行面
        # （api / command / local / workspace 及包本身）仍然禁止。
        allowed_sandbox = {"redpanda.sandbox.registry"}
        for path in sorted(CHANNELS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(_imports_any(modules, forbidden))
            leaked += sorted(
                module
                for module in _imports_any(modules, {"redpanda.sandbox"})
                if module not in allowed_sandbox
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(CHANNELS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_assistant_uses_only_its_explicit_tool_ports(self):
        allowed = {
            "builtin_tools.py": {
                "redpanda.tools.builtin",
                "redpanda.tools.executor",
                "redpanda.tools.registry",
            },
            "decision.py": {"redpanda.tools.builtin"},
            "runner.py": {"redpanda.tools.builtin"},
            "cli.py": {"redpanda.tools.spec"},
            "skills.py": {"redpanda.tools.spec"},
            "tool_results.py": {"redpanda.tools.control"},
            "work_plan.py": {
                "redpanda.tools.executor",
                "redpanda.tools.registry",
                "redpanda.tools.spec",
            },
            "control.py": {
                "redpanda.tools.control",
                "redpanda.tools.spec",
            },
            "management.py": {
                "redpanda.tools.control",
                "redpanda.tools.spec",
            },
        }
        offenders: list[str] = []
        for path in sorted(ASSISTANT_ROOT.rglob("*.py")):
            relative = str(path.relative_to(ASSISTANT_ROOT)).replace("\\", "/")
            actual = {
                module
                for module in _imported_modules(path)
                if module == "redpanda.tools" or module.startswith("redpanda.tools.")
            }
            unexpected = sorted(actual - allowed.get(relative, set()))
            if unexpected:
                offenders.append(f"{relative}: {', '.join(unexpected)}")
        self.assertEqual(offenders, [])

    def test_tools_do_not_import_runtime_or_product_layers(self):
        offenders: list[str] = []
        forbidden = {
            "redpanda.assistant",
            "redpanda.channels",
            "redpanda.cli",
            "redpanda.llm",
            "redpanda.mcp",
            "redpanda.runtime",
            "redpanda.skills",
        }
        for path in sorted(TOOLS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(_imports_any(modules, forbidden))
            if leaked:
                offenders.append(
                    f"{path.relative_to(TOOLS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_tools_do_not_import_sandbox_implementations(self):
        offenders: list[str] = []
        for path in sorted(TOOLS_ROOT.rglob("*.py")):
            leaked = sorted(
                _imports_any(
                    _imported_modules(path),
                    {"redpanda.sandbox.local"},
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(TOOLS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_runtime_does_not_import_product_layers(self):
        offenders: list[str] = []
        for path in sorted(RUNTIME_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(
                {
                    module
                    for module in modules
                    if module == "redpanda"
                    or (
                        module.startswith("redpanda.")
                        and not (
                            module == "redpanda.runtime"
                            or module.startswith("redpanda.runtime.")
                        )
                    )
                }
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(RUNTIME_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_sandbox_does_not_import_product_or_runtime_layers(self):
        offenders: list[str] = []
        forbidden = {
            "redpanda.assistant",
            "redpanda.channels",
            "redpanda.cli",
            "redpanda.llm",
            "redpanda.mcp",
            "redpanda.runtime",
            "redpanda.skills",
            "redpanda.tools",
        }
        for path in SANDBOX_ROOT.rglob("*.py"):
            modules = _imported_modules(path)
            leaked = sorted(_imports_any(modules, forbidden))
            if leaked:
                offenders.append(
                    f"{path.relative_to(SANDBOX_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])
