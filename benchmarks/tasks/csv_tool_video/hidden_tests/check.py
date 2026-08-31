from __future__ import annotations

import subprocess
import sys
from pathlib import Path


workspace = Path(sys.argv[1]).resolve()
program = workspace / "csv_tool.py"


def run_csv(content: str, *arguments: str) -> tuple[int, list[str], str]:
    source = workspace / ".hidden_csv_input.csv"
    try:
        source.write_text(content, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, str(program), str(source), *arguments],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        return completed.returncode, lines, completed.stderr
    finally:
        source.unlink(missing_ok=True)


cases = [
    ("empty age", "name,age\nAlice,20\nBroken,\nBob,17\n", (), ["Alice,20", "Bob,17"]),
    ("non-numeric age", "name,age\nAlice,20\nBroken,abc\nBob,17\n", (), ["Alice,20", "Bob,17"]),
    ("mixed records", "name,age\nBad,abc\nAlice,20\nEmpty,\nCara,31\n", (), ["Alice,20", "Cara,31"]),
    ("minimum age", "name,age\nAmy,17\nBen,20\nCara,25\n", ("--min-age", "20"), ["Ben,20", "Cara,25"]),
    ("default behavior", "name,age\nAlice,20\nBob,17\n", (), ["Alice,20", "Bob,17"]),
]

for label, content, arguments, expected in cases:
    code, output, stderr = run_csv(content, *arguments)
    assert code == 0, f"{label}: exit={code}, stderr={stderr}"
    assert output == expected, f"{label}: expected {expected}, got {output}"

print("hidden csv evaluator: PASS (5/5)")
