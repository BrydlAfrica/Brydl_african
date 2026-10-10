#!/usr/bin/env python3
"""AST check to ensure no blocking calls exist inside async functions.

Scans all Python files under redlab/ and verifies that inside `async def`:
  1. No `requests.*` calls exist (use `httpx.AsyncClient` or async I/O).
  2. No `time.sleep(...)` calls exist (use `asyncio.sleep` instead).
  3. No raw blocking calls exist.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
REDLAB_DIR = ROOT / "redlab"

violations: list[str] = []


class AsyncBlockingVisitor(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.current_async_func: str | None = None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        prev = self.current_async_func
        self.current_async_func = node.name
        self.generic_visit(node)
        self.current_async_func = prev

    def visit_Call(self, node: ast.Call) -> None:
        if self.current_async_func is not None:
            # Check for requests.*
            if isinstance(node.func, ast.Attribute):
                val = node.func.value
                attr = node.func.attr
                # requests.get / requests.post / etc.
                if isinstance(val, ast.Name) and val.id == "requests":
                    violations.append(
                        f"{self.filename}:{node.lineno}: in async def "
                        f"'{self.current_async_func}': blocking call "
                        f"'requests.{attr}()' found. Use httpx.AsyncClient."
                    )
                # time.sleep(...)
                if (isinstance(val, ast.Name) and val.id == "time"
                        and attr == "sleep"):
                    violations.append(
                        f"{self.filename}:{node.lineno}: in async def "
                        f"'{self.current_async_func}': blocking call "
                        f"'time.sleep()' found. Use asyncio.sleep instead."
                    )
            # Direct sleep(...) if imported `from time import sleep`
            elif isinstance(node.func, ast.Name):
                if node.func.id == "sleep":
                    violations.append(
                        f"{self.filename}:{node.lineno}: in async def "
                        f"'{self.current_async_func}': call to sleep() found."
                    )

        self.generic_visit(node)


def check_directory(dir_path: Path) -> None:
    for py_file in dir_path.rglob("*.py"):
        try:
            tree = ast.parse(
                py_file.read_text(encoding="utf-8"),
                filename=str(py_file),
            )
            visitor = AsyncBlockingVisitor(str(py_file.relative_to(ROOT)))
            visitor.visit(tree)
        except SyntaxError as e:
            violations.append(f"{py_file}: syntax error: {e}")


def main() -> int:
    if not REDLAB_DIR.is_dir():
        print(f"Error: {REDLAB_DIR} not found", file=sys.stderr)
        return 1

    check_directory(REDLAB_DIR)

    if violations:
        print("ASYNC BLOCKING CHECKS FAILED:")
        for v in violations:
            print(f"  - {v}")
        return 1

    print(
        "ALL ASYNC BLOCKING CHECKS PASSED: no blocking requests/sleep calls "
        "in async functions."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
