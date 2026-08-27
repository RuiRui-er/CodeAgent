"""Workspace path guard and discrete command safety policy."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


SAFE = "SAFE"
CONFIRM = "CONFIRM"
DENY = "DENY"


class WorkspaceGuard:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)

    def resolve(self, relative: str = ".") -> Path:
        raw = Path(relative)
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            candidate = (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes workspace: {relative!r}") from exc
        return candidate

    def command_path_violation(self, arguments: list[str]) -> str | None:
        for argument in arguments[1:]:
            token = argument.strip('"\'')
            if not token or token.startswith("-"):
                continue
            path = Path(token)
            if ".." in path.parts:
                return f"parent path traversal is not allowed: {argument}"
            if path.is_absolute():
                try:
                    path.resolve().relative_to(self.root)
                except ValueError:
                    return f"absolute path is outside workspace: {argument}"
        return None


@dataclass(frozen=True)
class CommandDecision:
    policy: str
    reason: str
    argv: list[str]
    display_command: str


class CommandPolicy:
    DENIED_PROGRAMS = {
        "sudo", "shutdown", "reboot", "poweroff", "halt", "mkfs", "format",
        "diskpart", "bcdedit", "reg", "regedit", "sc", "systemctl",
    }
    CONFIRM_PROGRAMS = {"rm", "del", "erase", "rmdir", "rd", "mv", "move"}
    SAFE_PROGRAMS = {
        "python", "python3", "py", "pytest", "gcc", "g++", "clang", "clang++",
        "javac", "java", "make", "cmake", "ninja", "node", "cargo", "go", "dotnet",
    }
    SAFE_GIT = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files"}
    CONFIRM_GIT = {"clean", "checkout", "restore", "reset", "switch", "rebase"}
    SHELL_OPERATORS = {"|", "||", "&&", ";", ">", ">>", "<"}

    def __init__(self, guard: WorkspaceGuard):
        self.guard = guard

    def classify(self, command: str | list[str]) -> CommandDecision:
        argv = self._parse(command)
        display = command if isinstance(command, str) else " ".join(command)
        if not argv:
            return CommandDecision(DENY, "command is empty", [], display)

        program = Path(argv[0].strip('"')).name.lower()
        if program.endswith(".exe"):
            program = program[:-4]
        denied_token = next(
            (Path(item.strip('"')).name.lower().removesuffix(".exe") for item in argv if Path(item.strip('"')).name.lower().removesuffix(".exe") in self.DENIED_PROGRAMS),
            None,
        )
        if denied_token:
            return CommandDecision(DENY, f"system-level command is outside the agent boundary: {denied_token}", argv, display)

        executable = Path(argv[0].strip('"'))
        if executable.is_absolute() and program not in self.SAFE_PROGRAMS and program != "git":
            try:
                executable.resolve().relative_to(self.guard.root)
            except ValueError:
                return CommandDecision(DENY, "executable path is outside the workspace and not on the safe development list", argv, display)

        violation = self.guard.command_path_violation(argv)
        if violation:
            return CommandDecision(DENY, violation, argv, display)
        if any(token in self.SHELL_OPERATORS for token in argv):
            return CommandDecision(CONFIRM, "complex shell syntax cannot be reliably analyzed", argv, display)
        if program in self.CONFIRM_PROGRAMS:
            return CommandDecision(CONFIRM, f"{program} may move or delete workspace files", argv, display)
        if program == "git":
            subcommand = self._first_positional(argv[1:])
            if subcommand in self.SAFE_GIT:
                return CommandDecision(SAFE, f"git {subcommand} is read-only", argv, display)
            if subcommand in self.CONFIRM_GIT:
                return CommandDecision(CONFIRM, f"git {subcommand} may overwrite workspace state", argv, display)
            return CommandDecision(CONFIRM, "git command may change repository state", argv, display)
        if self._is_dependency_change(program, argv):
            return CommandDecision(CONFIRM, "dependency installation or removal changes the environment", argv, display)
        if program in {"pip", "pip3"} and any(item.lower() in {"show", "list", "--version", "-v"} for item in argv[1:]):
            return CommandDecision(SAFE, "recognized read-only dependency query", argv, display)
        if program in self.SAFE_PROGRAMS:
            return CommandDecision(SAFE, "recognized development or verification command", argv, display)
        if program in {"npm", "pnpm", "yarn"} and any(item in argv[1:] for item in ("test", "run", "build")):
            return CommandDecision(SAFE, "recognized development script command", argv, display)
        return CommandDecision(CONFIRM, "unrecognized command requires explicit user confirmation", argv, display)

    def allowed_in_planning(self, decision: CommandDecision) -> bool:
        if decision.policy != SAFE or not decision.argv:
            return False
        program = Path(decision.argv[0].strip('"')).name.lower().removesuffix(".exe")
        if program == "git":
            return self._first_positional(decision.argv[1:]) in self.SAFE_GIT
        if program in {"python", "python3", "py", "pytest", "pip", "pip3"}:
            return True
        return any(item.lower() in {"--version", "-version", "-v"} for item in decision.argv[1:])

    @staticmethod
    def _parse(command: str | list[str]) -> list[str]:
        if isinstance(command, list):
            if not all(isinstance(item, str) for item in command):
                raise ValueError("command array must contain only strings")
            return command
        if not isinstance(command, str):
            raise ValueError("command must be a string or string array")
        parts = shlex.split(command, posix=os.name != "nt")
        return [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part for part in parts]

    @staticmethod
    def _first_positional(arguments: list[str]) -> str:
        return next((item.lower() for item in arguments if not item.startswith("-")), "")

    @staticmethod
    def _is_dependency_change(program: str, argv: list[str]) -> bool:
        lowered = [item.lower() for item in argv]
        actions = {"install", "uninstall", "remove", "add"}
        if program in {"pip", "pip3", "uv", "npm", "pnpm", "yarn", "conda", "poetry"}:
            return bool(actions.intersection(lowered[1:]))
        return program in {"python", "python3", "py"} and "-m" in lowered and "pip" in lowered and bool(actions.intersection(lowered))
