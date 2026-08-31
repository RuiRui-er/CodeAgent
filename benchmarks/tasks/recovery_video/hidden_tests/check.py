from __future__ import annotations

import subprocess
import sys
from pathlib import Path


program = Path(sys.argv[1]).resolve() / "label_tool.py"


def run(*arguments: str) -> str:
    completed = subprocess.run(
        [sys.executable, str(program), *arguments], capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


assert run("alice") == "alice"
assert run("alice", "--prefix", "VIP") == "VIP:alice"
print("hidden recovery evaluator: PASS (2/2)")
