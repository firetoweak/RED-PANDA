from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
BROAD = {"Exception", "BaseException"}
# 宽泛捕获且不再抛出，等于把未知异常降级为普通结果；只允许在登记的边界这样做。
BOUNDARIES = {
    "redpanda/assistant/host/llm_port.py::complete_llm_chat": "LLM 调用失败编码后跨进程交还调用方",
    "redpanda/assistant/host/supervisor.py::_watch": "Worker 失败归 Worker 边界，转为 WorkerFailed",
    "redpanda/mcp/client_manager.py::aclose": "逐个关闭连接，收集后合并抛出",
    "redpanda/tools/builtin/command_interrupts.py::wait": "收集各命令失败，由调用方抛出",
    "redpanda/tools/executor.py::_execute_spec": "handler 异常作为工具错误交给模型，不打崩 Worker",
}


def _swallowing_handlers(path: Path) -> list[str]:
    found = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.ExceptHandler):
            caught = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            broad = node.type is None or any(isinstance(name, ast.Name) and name.id in BROAD for name in caught)
            if broad and not any(isinstance(child, ast.Raise) for child in ast.walk(node)):
                found.append((function, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


class ExceptionBoundaryTest(unittest.TestCase):
    def test_broad_except_reraises_outside_declared_boundaries(self):
        offenders = []
        declared = set()
        for package in ("redpanda", "thinllm"):
            for path in sorted((ROOT / package).rglob("*.py")):
                relative = path.relative_to(ROOT).as_posix()
                for function, line in _swallowing_handlers(path):
                    key = f"{relative}::{function}"
                    if key in BOUNDARIES:
                        declared.add(key)
                    else:
                        offenders.append(f"{relative}:{line} in {function}")
        self.assertEqual(offenders, [])
        self.assertEqual(sorted(BOUNDARIES.keys() - declared), [], "登记的边界已不再宽泛捕获，删除该条目")
