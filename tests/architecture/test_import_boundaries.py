from __future__ import annotations

import ast
from pathlib import Path
import unittest


RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "konden" / "runtime"
ASSISTANT_ROOT = Path(__file__).resolve().parents[2] / "konden" / "assistant"
LLM_ROOT = Path(__file__).resolve().parents[2] / "konden" / "llm"
MCP_ROOT = Path(__file__).resolve().parents[2] / "konden" / "mcp"
SKILLS_ROOT = Path(__file__).resolve().parents[2] / "konden" / "skills"
CLI_ROOT = Path(__file__).resolve().parents[2] / "konden" / "cli"
TOOLS_ROOT = Path(__file__).resolve().parents[2] / "konden" / "tools"
FILESYSTEM_ROOT = Path(__file__).resolve().parents[2] / "konden" / "filesystem"
EXECUTION_ROOT = Path(__file__).resolve().parents[2] / "konden" / "execution"
CHANNELS_ROOT = Path(__file__).resolve().parents[2] / "konden" / "channels"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "konden" / "config.py"
BOOTSTRAP_PATH = Path(__file__).resolve().parents[2] / "konden" / "bootstrap.py"
THINLLM_ROOT = Path(__file__).resolve().parents[2] / "thinllm"


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = path.relative_to(RUNTIME_ROOT.parents[1]).with_suffix("")
                package = relative.parts[:-1]
                base = package[:len(package) - node.level + 1]
                module = ".".join((*base, *((node.module,) if node.module else ())))
            else:
                module = node.module
            if module:
                modules.add(module)
                for alias in node.names:
                    imported = f"{module}.{alias.name}"
                    candidate = RUNTIME_ROOT.parents[1].joinpath(*imported.split("."))
                    if candidate.with_suffix(".py").is_file() or (candidate / "__init__.py").is_file():
                        modules.add(imported)
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
                    {"konden.assistant", "konden.runtime"},
                )
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(SKILLS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_cli_does_not_import_runtime_or_product_layers(self):
        # CLI consumes command execution and tool contracts.
        offenders: list[str] = []
        forbidden = {
            "konden.assistant",
            "konden.channels",
            "konden.llm",
            "konden.mcp",
            "konden.runtime",
            "konden.skills",
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
                    {"konden.assistant", "konden.runtime"},
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
                        "konden.assistant",
                        "konden.channels",
                        "konden.cli",
                        "konden.mcp",
                        "konden.runtime",
                        "konden.skills",
                        "konden.tools",
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
                if module.startswith("konden.llm.")
                and module != "konden.llm.api"
            )
            if unexpected:
                offenders.append(
                    f"{path.relative_to(ASSISTANT_ROOT)}: {', '.join(unexpected)}"
                )
        self.assertEqual(offenders, [])

    def test_bootstrap_not_config_owns_the_concrete_llm_client(self):
        self.assertNotIn("thinllm", _imported_modules(CONFIG_PATH))
        self.assertIn("thinllm", _imported_modules(BOOTSTRAP_PATH))

    def test_thinllm_does_not_import_konden(self):
        offenders: list[str] = []
        for path in sorted(THINLLM_ROOT.rglob("*.py")):
            leaked = sorted(_imports_any(_imported_modules(path), {"konden"}))
            if leaked:
                offenders.append(
                    f"{path.relative_to(THINLLM_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_worker_config_does_not_construct_the_concrete_llm_client(self):
        import inspect

        from konden.bootstrap import worker_config

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
            "konden.llm",
            "konden.runtime",
            "konden.tools",
        }
        # Channels consume workspace registration, never execution or file internals.
        allowed_filesystem = {"konden.filesystem.registry"}
        for path in sorted(CHANNELS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(_imports_any(modules, forbidden))
            leaked += sorted(
                module
                for module in _imports_any(modules, {"konden.filesystem", "konden.execution"})
                if module not in allowed_filesystem
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(CHANNELS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_assistant_uses_only_its_explicit_tool_ports(self):
        allowed = {
            "builtin_tools.py": {
                "konden.tools.builtin",
                "konden.tools.executor",
                "konden.tools.registry",
            },
            "conversations.py": {"konden.tools.results"},
            "decision.py": {"konden.tools.builtin"},
            "runner.py": {"konden.tools.builtin"},
            "cli.py": {"konden.tools.spec"},
            "skills.py": {"konden.tools.spec"},
            "tool_results.py": {"konden.tools.control"},
            "work_plan.py": {
                "konden.tools.executor",
                "konden.tools.registry",
                "konden.tools.spec",
            },
            "control.py": {
                "konden.tools.control",
                "konden.tools.spec",
            },
            "management.py": {
                "konden.tools.control",
                "konden.tools.spec",
            },
        }
        offenders: list[str] = []
        for path in sorted(ASSISTANT_ROOT.rglob("*.py")):
            relative = str(path.relative_to(ASSISTANT_ROOT)).replace("\\", "/")
            actual = {
                module
                for module in _imported_modules(path)
                if module == "konden.tools" or module.startswith("konden.tools.")
            }
            unexpected = sorted(actual - allowed.get(relative, set()))
            if unexpected:
                offenders.append(f"{relative}: {', '.join(unexpected)}")
        self.assertEqual(offenders, [])

    def test_tools_do_not_import_runtime_or_product_layers(self):
        offenders: list[str] = []
        forbidden = {
            "konden.assistant",
            "konden.channels",
            "konden.cli",
            "konden.llm",
            "konden.mcp",
            "konden.runtime",
            "konden.skills",
        }
        for path in sorted(TOOLS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(_imports_any(modules, forbidden))
            if leaked:
                offenders.append(
                    f"{path.relative_to(TOOLS_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_tools_do_not_import_execution_implementations(self):
        offenders: list[str] = []
        for path in sorted(TOOLS_ROOT.rglob("*.py")):
            leaked = sorted(
                _imports_any(
                    _imported_modules(path),
                    {
                        "konden.execution.local",
                        "konden.execution.process_win",
                        "konden.execution.process_posix",
                    },
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
                    if module == "konden"
                    or (
                        module.startswith("konden.")
                        and not (
                            module == "konden.runtime"
                            or module.startswith("konden.runtime.")
                        )
                    )
                }
            )
            if leaked:
                offenders.append(
                    f"{path.relative_to(RUNTIME_ROOT)}: {', '.join(leaked)}"
                )
        self.assertEqual(offenders, [])

    def test_file_management_consumers_use_public_entry_points(self):
        """Internal file helpers stay behind the package entry point."""
        files_root = FILESYSTEM_ROOT
        offenders = []
        for path in FILESYSTEM_ROOT.parent.rglob("*.py"):
            if path.is_relative_to(files_root):
                continue
            implementations = sorted(
                module for module in _imported_modules(path)
                if module.startswith("konden.filesystem.")
                and module not in {"konden.filesystem.workspace", "konden.filesystem.registry"}
            )
            if implementations:
                offenders.append(f"{path}: {', '.join(implementations)}")
        self.assertEqual(offenders, [])

    def test_file_and_execution_capabilities_are_independent(self):
        offenders: list[str] = []
        forbidden = {
            "konden.assistant",
            "konden.channels",
            "konden.cli",
            "konden.llm",
            "konden.mcp",
            "konden.runtime",
            "konden.skills",
            "konden.tools",
        }
        for capability, other in ((FILESYSTEM_ROOT, "konden.execution"),
                                  (EXECUTION_ROOT, "konden.filesystem")):
            for path in capability.rglob("*.py"):
                leaked = sorted(_imports_any(_imported_modules(path), forbidden | {other}))
                if leaked:
                    offenders.append(f"{path.relative_to(capability.parent)}: {', '.join(leaked)}")
        self.assertEqual(offenders, [])
