"""A minimal coding agent built without an Agent framework or SDK."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """You are a small coding agent working inside one workspace.
Solve the user's task autonomously. Inspect relevant files before editing, make the
smallest useful change, and run an appropriate command or test to verify it.
Use only the provided tools. Never try to access anything outside the workspace.
When the task is complete, respond with a concise summary and verification result.
If a tool fails, inspect its error and decide how to recover.
"""


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory; default is workspace root."},
                    "recursive": {"type": "boolean", "description": "List recursively."},
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a UTF-8 text file in the workspace. Use the full desired content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search for a literal string in workspace text files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Relative file or directory; default is root."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a command in the workspace. Pass argv as a JSON array, without shell syntax.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["args"],
                "additionalProperties": False,
            },
        },
    },
]


class WorkspaceTools:
    """Local tools whose direct file operations are confined to one root."""

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)

    def _resolve(self, relative: str = ".") -> Path:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes workspace: {relative!r}") from exc
        return candidate

    def list_files(self, path: str = ".", recursive: bool = False) -> dict[str, Any]:
        base = self._resolve(path)
        if not base.is_dir():
            raise ValueError(f"not a directory: {path}")
        entries = base.rglob("*") if recursive else base.iterdir()
        items = []
        for item in sorted(entries):
            kind = "dir" if item.is_dir() else "file"
            items.append({"path": item.relative_to(self.root).as_posix(), "type": kind})
        return {"items": items}

    def read_file(self, path: str) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            raise ValueError(f"not a file: {path}")
        return {"path": path, "content": target.read_text(encoding="utf-8")}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": target.relative_to(self.root).as_posix(), "bytes": len(content.encode("utf-8"))}

    def search_text(self, query: str, path: str = ".") -> dict[str, Any]:
        base = self._resolve(path)
        files = [base] if base.is_file() else (p for p in base.rglob("*") if p.is_file())
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
                    return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def run_command(self, args: list[str], timeout: int = 30) -> dict[str, Any]:
        if not args or not all(isinstance(arg, str) for arg in args):
            raise ValueError("args must be a non-empty list of strings")
        timeout = max(1, min(int(timeout), 120))
        # shell=False prevents pipes, redirections, command chaining, and shell built-ins.
        completed = subprocess.run(
            args,
            cwd=self.root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "exit_code": completed.returncode,
        }

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        methods = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "write_file": self.write_file,
            "search_text": self.search_text,
            "run_command": self.run_command,
        }
        try:
            if name not in methods:
                raise ValueError(f"unknown tool: {name}")
            return {"ok": True, "result": methods[name](**arguments)}
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "error": f"command timed out after {exc.timeout} seconds"}
        except Exception as exc:  # Tool failures become observations for the model.
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


class OpenAICompatibleClient:
    """Tiny Chat Completions client implemented with the Python standard library."""

    def __init__(self) -> None:
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable is not set")

    def complete(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model API returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"model API request failed: {exc}") from exc
        try:
            return payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected model response: {payload}") from exc


def log(label: str, value: Any) -> None:
    print(f"\n=== {label} ===", flush=True)
    if isinstance(value, str):
        print(value, flush=True)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def run_agent(task: str, workspace: Path, max_steps: int) -> str:
    tools = WorkspaceTools(workspace)
    client = OpenAICompatibleClient()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]

    for step in range(1, max_steps + 1):
        log(f"STEP {step}/{max_steps} - MODEL REQUEST", {"message_count": len(messages)})
        message = client.complete(messages)
        messages.append(message)
        if message.get("content"):
            log("MODEL MESSAGE", message["content"])

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            final = message.get("content") or "Model stopped without a final message."
            log("AGENT FINISHED", final)
            return final

        for tool_call in tool_calls:
            name = tool_call.get("function", {}).get("name", "")
            raw_arguments = tool_call.get("function", {}).get("arguments", "{}")
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must decode to an object")
            except (json.JSONDecodeError, ValueError) as exc:
                arguments = {}
                result = {"ok": False, "error": f"invalid tool arguments: {exc}"}
            else:
                log("TOOL CALL", {"name": name, "arguments": arguments})
                result = tools.call(name, arguments)
            log("TOOL RESULT", result)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "content": json.dumps(result, ensure_ascii=False),
            })

    final = f"Stopped after reaching the maximum of {max_steps} steps."
    log("AGENT STOPPED", final)
    return final


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal coding agent baseline")
    parser.add_argument("task", nargs="?", help="Programming task. If omitted, read interactively.")
    parser.add_argument("--workspace", default=".", help="Workspace directory (default: current directory)")
    parser.add_argument("--max-steps", type=int, default=12, help="Maximum model turns (default: 12)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task = args.task or input("Programming task: ").strip()
    if not task:
        print("Task cannot be empty.", file=sys.stderr)
        return 2
    if args.max_steps < 1:
        print("--max-steps must be at least 1.", file=sys.stderr)
        return 2
    try:
        workspace = Path(args.workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace is not a directory")
        run_agent(task, workspace, args.max_steps)
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
