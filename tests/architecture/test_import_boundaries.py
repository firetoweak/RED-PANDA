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
FILESYSTEM_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "filesystem"
EXECUTION_ROOT = Path(__file__).resolve().parents[2] / "redpanda" / "execution"
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
                    {"redpanda.assistant", "redpanda.runtime"},
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
        # Channels consume workspace registration, never execution or file internals.
        allowed_filesystem = {"redpanda.filesystem.registry"}
        for path in sorted(CHANNELS_ROOT.rglob("*.py")):
            modules = _imported_modules(path)
            leaked = sorted(_imports_any(modules, forbidden))
            leaked += sorted(
                module
                for module in _imports_any(modules, {"redpanda.filesystem", "redpanda.execution"})
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
                "redpanda.tools.builtin",
                "redpanda.tools.executor",
                "redpanda.tools.registry",
            },
            "conversations.py": {"redpanda.tools.results"},
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

    def test_tools_do_not_import_execution_implementations(self):
        offenders: list[str] = []
        for path in sorted(TOOLS_ROOT.rglob("*.py")):
            leaked = sorted(
                _imports_any(
                    _imported_modules(path),
                    {
                        "redpanda.execution.local",
                        "redpanda.execution.process_win",
                        "redpanda.execution.process_posix",
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

    def test_file_management_consumers_use_public_entry_points(self):
        """Internal file helpers stay behind the package entry point."""
        files_root = FILESYSTEM_ROOT
        offenders = []
        for path in FILESYSTEM_ROOT.parent.rglob("*.py"):
            if path.is_relative_to(files_root):
                continue
            implementations = sorted(
                module for module in _imported_modules(path)
                if module.startswith("redpanda.filesystem.")
                and module not in {"redpanda.filesystem.workspace", "redpanda.filesystem.registry"}
            )
            if implementations:
                offenders.append(f"{path}: {', '.join(implementations)}")
        self.assertEqual(offenders, [])

    def test_file_and_execution_capabilities_are_independent(self):
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
        for capability, other in ((FILESYSTEM_ROOT, "redpanda.execution"),
                                  (EXECUTION_ROOT, "redpanda.filesystem")):
            for path in capability.rglob("*.py"):
                leaked = sorted(_imports_any(_imported_modules(path), forbidden | {other}))
                if leaked:
                    offenders.append(f"{path.relative_to(capability.parent)}: {', '.join(leaked)}")
        self.assertEqual(offenders, [])
