"""Deterministic model fixtures and frozen checks for the video demonstrations.

The fixtures choose actions, but never manufacture tool or verification results. Every
command, edit, transition, recovery, checkpoint, and hidden evaluation is executed by
the production CodeAgent components.
"""

from __future__ import annotations

import json
import sys
from typing import Any


CSV_TASK = (
    "修复 CSV 中缺失或非法 age 导致批处理失败的问题，并新增 --min-age N 筛选功能。"
    "异常记录不能影响其他合法记录，且未使用筛选参数时保持原有正常行为。"
)

RECOVERY_TASK = "Add --prefix TEXT to label_tool.py while preserving output when the option is omitted."


class ScriptedClient:
    """Small Chat Completions test double returning a fixed sequence of tool calls."""

    def __init__(self, responses: list[dict[str, Any]]):
        self._responses = iter(responses)
        self.calls = 0

    def complete(self, messages, tool_schemas=None):
        self.calls += 1
        return next(self._responses)


def tool_response(name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }],
    }


def csv_plan() -> dict[str, Any]:
    checks = csv_checks()
    criteria = [
        _criterion("AC_EMPTY", "缺失 age 不导致整个批处理崩溃，其他合法记录仍输出", "TARGET"),
        _criterion("AC_INVALID", "非数字 age 不影响其他合法记录", "TARGET"),
        _criterion("AC_MIN_AGE", "--min-age N 只输出 age >= N 的合法记录", "TARGET"),
        _criterion("AC_DEFAULT", "不使用 --min-age 时原有正常输入输出保持不变", "REGRESSION"),
        _criterion("AC_SANITY", "csv_tool.py 修改后仍可被 Python 编译", "SANITY"),
    ]
    all_ids = [item["id"] for item in criteria]
    return {
        "task_understanding": "容错解析每一行 age，跳过异常行，并增加可选的最小年龄过滤。",
        "acceptance_criteria": criteria,
        "verification_contract": checks,
        "execution_plan": [
            {
                "step_id": "STEP_INSPECT", "description": "Inspect csv_tool.py before editing",
                "step_kind": "INSPECT", "suggested_tools": ["read_file"],
                "related_acceptance_criteria": all_ids,
                "expected_change_files": [], "related_verification_ids": [],
            },
            {
                "step_id": "STEP_IMPLEMENT", "description": "修改 csv_tool.py：逐行跳过缺失或非法 age，并新增可选的 --min-age 筛选",
                "step_kind": "IMPLEMENT", "suggested_tools": ["apply_patch"],
                "related_acceptance_criteria": ["AC_EMPTY", "AC_INVALID", "AC_MIN_AGE", "AC_DEFAULT"],
                "expected_change_files": ["csv_tool.py"], "related_verification_ids": [],
            },
            {
                "step_id": "STEP_VERIFY", "description": "运行已冻结的 SANITY、TARGET 与 REGRESSION 检查",
                "step_kind": "VERIFY", "suggested_tools": ["run_command", "finish"],
                "related_acceptance_criteria": all_ids,
                "expected_change_files": [],
                "related_verification_ids": [item["id"] for item in checks],
            },
        ],
        "clarification_needed": None,
    }


def csv_checks() -> list[dict[str, Any]]:
    return [
        _check(
            "V_SANITY", "Compile csv_tool.py", "SANITY", ["AC_SANITY"],
            [sys.executable, "-m", "py_compile", "csv_tool.py"],
            "python -m py_compile csv_tool.py must exit 0",
        ),
        _check(
            "V_EMPTY", "Malformed input · empty age", "TARGET", ["AC_EMPTY"],
            _csv_command("name,age\nAlice,20\nBroken,\nBob,17\n", [], ["Alice,20", "Bob,17"]),
            "Run mixed empty-age CSV and require Alice,20 and Bob,17 with exit 0",
        ),
        _check(
            "V_INVALID", "Malformed input · non-numeric age", "TARGET", ["AC_INVALID"],
            _csv_command("name,age\nAlice,20\nBroken,abc\nBob,17\n", [], ["Alice,20", "Bob,17"]),
            "Run mixed non-numeric-age CSV and require both legal rows with exit 0",
        ),
        _check(
            "V_MIN_AGE", "--min-age 20", "TARGET", ["AC_MIN_AGE"],
            _csv_command(
                "name,age\nAmy,17\nBen,20\nCara,25\n", ["--min-age", "20"], ["Ben,20", "Cara,25"],
            ),
            "Run --min-age 20 and require only Ben,20 and Cara,25",
        ),
        _check(
            "V_DEFAULT", "Normal input without --min-age", "REGRESSION", ["AC_DEFAULT"],
            _csv_command("name,age\nAlice,20\nBob,17\n", [], ["Alice,20", "Bob,17"]),
            "Run normal CSV without --min-age and require the exact original output",
        ),
    ]


def csv_planning_client() -> ScriptedClient:
    return ScriptedClient([tool_response("submit_plan", csv_plan(), "csv-plan")])


def csv_execution_client() -> ScriptedClient:
    main_before = '''def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file")
    args = parser.parse_args()

    with Path(args.csv_file).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name, age = parse_row(row)
            print(f"{name},{age}")
    return 0
'''
    main_after = '''def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file")
    parser.add_argument("--min-age", type=int)
    args = parser.parse_args()

    with Path(args.csv_file).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                name, age = parse_row(row)
            except (KeyError, TypeError, ValueError):
                continue
            if args.min_age is None or age >= args.min_age:
                print(f"{name},{age}")
    return 0
'''
    return ScriptedClient([
        tool_response("read_file", {"path": "csv_tool.py"}, "csv-read"),
        tool_response("apply_patch", {
            "file": "csv_tool.py", "operation": "replace",
            "intent": "add optional minimum-age filtering while preserving default output", "symbol": "main",
            "old_block": main_before, "new_block": main_after,
        }, "csv-filter-edit"),
        tool_response("finish", {"summary": "Implementation is ready for frozen final verification."}, "csv-finish"),
    ])


def recovery_plan() -> dict[str, Any]:
    checks = recovery_checks()
    criteria = [
        _criterion("AC_PREFIX", "--prefix TEXT outputs TEXT:name", "TARGET"),
        _criterion("AC_DEFAULT", "Without --prefix, output remains the original name", "REGRESSION"),
        _criterion("AC_SANITY", "label_tool.py remains compilable", "SANITY"),
    ]
    all_ids = [item["id"] for item in criteria]
    return {
        "task_understanding": "Add an optional prefix without changing default output.",
        "acceptance_criteria": criteria,
        "verification_contract": checks,
        "execution_plan": [{
            "step_id": "STEP_IMPLEMENT", "description": "Add the optional --prefix behavior",
            "step_kind": "IMPLEMENT", "suggested_tools": ["apply_patch"],
            "related_acceptance_criteria": all_ids,
            "expected_change_files": ["label_tool.py"], "related_verification_ids": [],
        }, {
            "step_id": "STEP_VERIFY", "description": "Run frozen prefix, default, and compile checks",
            "step_kind": "VERIFY", "suggested_tools": ["run_command", "finish"],
            "related_acceptance_criteria": all_ids, "expected_change_files": [],
            "related_verification_ids": [item["id"] for item in checks],
        }],
        "clarification_needed": None,
    }


def recovery_checks() -> list[dict[str, Any]]:
    return [
        _check(
            "V_SANITY", "Compile label_tool.py", "SANITY", ["AC_SANITY"],
            [sys.executable, "-m", "py_compile", "label_tool.py"],
            "python -m py_compile label_tool.py must exit 0",
        ),
        _check(
            "V_PREFIX", "--prefix VIP", "TARGET", ["AC_PREFIX"],
            _cli_command("label_tool.py", ["alice", "--prefix", "VIP"], "VIP:alice"),
            "Run alice --prefix VIP and require VIP:alice",
        ),
        _check(
            "V_DEFAULT", "Default label output", "REGRESSION", ["AC_DEFAULT"],
            _cli_command("label_tool.py", ["alice"], "alice"),
            "Run alice without --prefix and require the original output alice",
        ),
    ]


RECOVERY_MAIN_BEFORE = '''def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    args = parser.parse_args()
    print(args.name)
    return 0
'''

RECOVERY_MAIN_REGRESSED = '''def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--prefix")
    args = parser.parse_args()
    print(f"{args.prefix}:{args.name}")
    return 0
'''

RECOVERY_MAIN_FIXED = '''def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--prefix")
    args = parser.parse_args()
    print(f"{args.prefix}:{args.name}" if args.prefix else args.name)
    return 0
'''


def _criterion(identifier: str, description: str, evidence_type: str) -> dict[str, Any]:
    return {
        "id": identifier, "description": description, "criticality": "CRITICAL",
        "verification_mode": "AUTO", "evidence_type": evidence_type,
        "verification_method": f"Run the frozen {evidence_type} command for {identifier} and require exit code 0",
    }


def _check(
    identifier: str,
    description: str,
    evidence_type: str,
    criteria: list[str],
    command: list[str],
    method: str,
) -> dict[str, Any]:
    return {
        "id": identifier, "description": description, "verification_mode": "AUTO",
        "evidence_type": evidence_type, "verification_method": method, "command": command,
        "baseline_required": True, "related_acceptance_criteria": criteria,
    }


def _csv_command(content: str, arguments: list[str], expected: list[str]) -> list[str]:
    script = (
        "import pathlib,subprocess,sys;"
        f"content={content!r};args={arguments!r};expected={expected!r};"
        "p=pathlib.Path('.video_verification_input.csv');"
        "p.write_text(content,encoding='utf-8');"
        "r=subprocess.run([sys.executable,'csv_tool.py',str(p),*args],capture_output=True,text=True);"
        "p.unlink(missing_ok=True);"
        "actual=[x.strip() for x in r.stdout.splitlines() if x.strip()];"
        "assert r.returncode==0,(r.returncode,r.stderr);assert actual==expected,(actual,expected)"
    )
    return [sys.executable, "-c", script]


def _cli_command(program: str, arguments: list[str], expected: str) -> list[str]:
    script = (
        "import subprocess,sys;"
        f"r=subprocess.run([sys.executable,{program!r},*{arguments!r}],capture_output=True,text=True);"
        f"assert r.returncode==0,(r.returncode,r.stderr);assert r.stdout.strip()=={expected!r},r.stdout"
    )
    return [sys.executable, "-c", script]
