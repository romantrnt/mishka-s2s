"""Offline guardrails: parse all source and enforce clean dependency direction."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1] / "app" / "foundation"
for path in root.glob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    if path.name in {"domain.py", "application.py"}:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(("fastapi", "sqlite3", "serial", "app.mvp", "torch", "TTS")):
                raise SystemExit(f"forbidden external dependency in core: {path.name}: {node.module}")
print("foundation lint: parsed source and dependency boundaries are clean")
