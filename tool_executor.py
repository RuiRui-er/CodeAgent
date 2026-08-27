"""Workspace-scoped tool execution with explicit safety boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

from agent_state import PLANNING, VERIFYING, AgentState
from tool_registry import TOOLS, permission_result
from tool_safety import CONFIRM, DENY, CommandPolicy, WorkspaceGuard


MAX_COMMAND_OUTPUT_CHARS = 8_000


def truncate_command_output(text: str, limit: int = MAX_COMMAND_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    omitted_chars = len(text) - limit
    omitted_lines = 0
    for _ in range(4):
        marker = f"\n[... omitted {omitted_chars} chars / {omitted_lines} lines ...]\n"
        available = max(0, limit - len(marker))
        head = available // 2
        tail = available - head
        omitted = text[head:len(text) - tail]
        omitted_chars = len(omitted)
        omitted_lines = omitted.count("\n")
    marker = f"\n[... omitted {omitted_chars} chars / {omitted_lines} lines ...]\n"
    available = max(0, limit - len(marker))
    head = available // 2
    tail = available - head
    return text[:head] + marker + text[-tail:], True


class ToolExecutor:
    def __init__(self, root: Path, confirm_callback: Callable[[str, str], bool] | None = None):
        self.guard = WorkspaceGuard(root)
        self.root = self.guard.root
        self.command_policy = CommandPolicy(self.guard)
        self.confirm_callback = confirm_callback or self._confirm_with_input

    def call(self, state: AgentState, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        request_phase = state.current_phase
        blocked = permission_result(request_phase, name)
        if blocked:
            self._log_tool(request_phase, name, blocked)
            return blocked
        try:
            if name == "read_file":
                result = self.read_file(**arguments)
            elif name == "list_dir":
                result = self.list_dir(**arguments)
            elif name == "search_code":
                result = self.search_code(**arguments)
            elif name == "apply_patch":
                result = self.apply_patch(**arguments)
            elif name == "write_file":
                result = self.write_file(**arguments)
            elif name == "run_command":
                result = self.run_command(planning=request_phase == PLANNING, **arguments)
            elif name == "finish":
                state.set_phase(VERIFYING)
                result = {
                    "status": "SUCCESS",
                    "tool": name,
                    "category": TOOLS[name].category,
                    "summary": arguments.get("summary", ""),
                    "next_phase": VERIFYING,
                    "done": False,
                }
            else:
                result = {"status": "BLOCKED", "tool": name, "reason": "unknown tool"}
        except Exception as exc:
            result = {
                "status": "FAILED",
                "tool": name,
                "category": TOOLS[name].category,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        self._log_tool(request_phase, name, result)
        return result

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> dict[str, Any]:
        target = self.guard.resolve(path)
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        start = start_line or 1
        end = end_line or len(lines)
        if start < 1 or end < start:
            raise ValueError("line range must satisfy 1 <= start_line <= end_line")
        content = "".join(lines[start - 1:end])
        return {
            "status": "SUCCESS",
            "tool": "read_file",
            "category": TOOLS["read_file"].category,
            "path": target.relative_to(self.root).as_posix(),
            "start_line": start,
            "end_line": min(end, len(lines)),
            "content": content,
        }

    def list_dir(self, path: str = ".", max_depth: int = 2) -> dict[str, Any]:
        base = self.guard.resolve(path)
        if not base.is_dir():
            raise ValueError(f"not a directory: {path}")
        depth_limit = max(1, min(int(max_depth), 8))
        items = []
        for item in sorted(base.rglob("*")):
            depth = len(item.relative_to(base).parts)
            if depth > depth_limit:
                continue
            items.append({
                "path": item.relative_to(self.root).as_posix(),
                "type": "dir" if item.is_dir() else "file",
            })
            if len(items) >= 500:
                break
        return {
            "status": "SUCCESS",
            "tool": "list_dir",
            "category": TOOLS["list_dir"].category,
            "items": items,
            "truncated": len(items) >= 500,
        }

    def search_code(self, query: str, path: str = ".") -> dict[str, Any]:
        base = self.guard.resolve(path)
        files = [base] if base.is_file() else (item for item in base.rglob("*") if item.is_file())
        matches: list[dict[str, Any]] = []
        for file in files:
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(lines, 1):
                if query in line:
                    matches.append({
                        "path": file.relative_to(self.root).as_posix(),
                        "line": number,
                        "text": line,
                    })
                if len(matches) >= 200:
                    return self._search_result(matches, True)
        return self._search_result(matches, False)

    def apply_patch(self, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        if not old_text:
            raise ValueError("old_text cannot be empty")
        target = self.guard.resolve(path)
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        content = target.read_text(encoding="utf-8")
        occurrences = content.count(old_text)
        if occurrences != 1:
            raise ValueError(f"old_text must match exactly once; found {occurrences}")
        target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
        return {
            "status": "SUCCESS",
            "tool": "apply_patch",
            "category": TOOLS["apply_patch"].category,
            "path": target.relative_to(self.root).as_posix(),
            "replacements": 1,
        }

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        target = self.guard.resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "status": "SUCCESS",
            "tool": "write_file",
            "category": TOOLS["write_file"].category,
            "path": target.relative_to(self.root).as_posix(),
            "bytes": len(content.encode("utf-8")),
        }

    def run_command(self, command: str | list[str], timeout: int = 60, planning: bool = False) -> dict[str, Any]:
        decision = self.command_policy.classify(command)
        base = {
            "tool": "run_command",
            "category": TOOLS["run_command"].category,
            "command": decision.display_command,
            "policy": decision.policy,
            "policy_reason": decision.reason,
        }
        if decision.policy == DENY:
            return {
                **base,
                "status": "DENIED",
                "reason": decision.reason,
                "alternative": "Use a workspace-scoped development command.",
            }
        if planning and not self.command_policy.allowed_in_planning(decision):
            return {
                **base,
                "status": "DENIED",
                "reason": "PLANNING only allows tests and read-only development queries",
                "alternative": "Use read_file, list_dir, search_code, or a read-only test/query command.",
            }
        if decision.policy == CONFIRM:
            print("\n[Tool Safety]", flush=True)
            print(f"Command: {decision.display_command}", flush=True)
            print("Policy: CONFIRM", flush=True)
            print(f"Reason: {decision.reason}", flush=True)
            if not self.confirm_callback(decision.display_command, decision.reason):
                return {**base, "status": "DENIED", "reason": "command rejected by user"}

        timeout = max(1, min(int(timeout), 120))
        try:
            completed = subprocess.run(
                decision.argv,
                cwd=self.root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raw_stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            raw_stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stdout, stdout_truncated = truncate_command_output(raw_stdout)
            stderr, stderr_truncated = truncate_command_output(raw_stderr)
            return {
                **base,
                "status": "TIMEOUT",
                "exit_code": None,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": stdout_truncated or stderr_truncated,
                "timeout": timeout,
            }
        except OSError as exc:
            return {**base, "status": "FAILED", "exit_code": None, "stdout": "", "stderr": str(exc), "truncated": False}

        stdout, stdout_truncated = truncate_command_output(completed.stdout)
        stderr, stderr_truncated = truncate_command_output(completed.stderr)
        return {
            **base,
            "status": "SUCCESS" if completed.returncode == 0 else "FAILED",
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }

    @staticmethod
    def _search_result(matches: list[dict[str, Any]], truncated: bool) -> dict[str, Any]:
        return {
            "status": "SUCCESS",
            "tool": "search_code",
            "category": TOOLS["search_code"].category,
            "matches": matches,
            "truncated": truncated,
        }

    @staticmethod
    def _confirm_with_input(command: str, reason: str) -> bool:
        answer = input(f"Execute command? {command}\nRisk: {reason}\nConfirm [y/N]: ").strip().lower()
        return answer in {"y", "yes"}

    @staticmethod
    def _log_tool(phase: str, name: str, result: dict[str, Any]) -> None:
        print("\n[Tool]", flush=True)
        print(f"Phase: {phase}", flush=True)
        print(f"Name: {name}", flush=True)
        if result.get("policy"):
            print(f"Risk: {result['policy']}", flush=True)
        if result.get("command"):
            print(f"Command: {result['command']}", flush=True)
        print(f"Status: {result.get('status', 'UNKNOWN')}", flush=True)
        if result.get("exit_code") is not None:
            print(f"Exit code: {result['exit_code']}", flush=True)
        if result.get("reason") and result.get("status") != "SUCCESS":
            print(f"Reason: {result['reason']}", flush=True)
