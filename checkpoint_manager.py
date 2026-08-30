"""ChangeSet recovery and verification-linked stable Git checkpoints."""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent_state import AgentState
from edit_models import (
    APPLIED,
    CHECKPOINT_ROLLED_BACK,
    PARTIALLY_VERIFIED,
    REGRESSED,
    ROLLBACK_NONE,
    UNDONE,
    UNVERIFIED,
    VERIFIED,
)


UNSAFE_TO_UNDO = "UNSAFE_TO_UNDO"
UNEXPECTED_WORKSPACE_CHANGE = "UNEXPECTED_WORKSPACE_CHANGE"
WORKSPACE_DIRTY = "WORKSPACE_DIRTY"
CHECKPOINT_UNAVAILABLE = "CHECKPOINT_UNAVAILABLE"


@dataclass(frozen=True)
class StableCheckpoint:
    id: str
    commit_hash: str
    reason: str
    created_step: str | None
    linked_changesets: list[str]
    verification_ref: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CheckpointManager:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve(strict=True)
        self.checkpoint_available = False
        self.unavailable_reason: str | None = None
        self.checkpoints: list[StableCheckpoint] = []
        self.pending_changesets: list[str] = []
        self._registered: dict[str, dict[str, Any]] = {}

    def initialize(self) -> dict[str, Any]:
        top_level = self._git(["rev-parse", "--show-toplevel"], check=False)
        if top_level.returncode != 0:
            return self._unavailable("workspace is not a Git repository")
        repo_root = Path(top_level.stdout.strip()).resolve()
        if repo_root != self.workspace:
            return self._unavailable("target workspace is not the Git repository root")
        status = self._actionable_status_paths()
        if status:
            return self._unavailable("Stable checkpoint initialization refused: working tree is dirty", WORKSPACE_DIRTY)
        head = self._head()
        if not head:
            return self._unavailable("Git repository has no HEAD commit")

        checkpoint = StableCheckpoint(
            id="checkpoint_000",
            commit_hash=head,
            reason="initial clean workspace state",
            created_step=None,
            linked_changesets=[],
            verification_ref=None,
        )
        self.checkpoint_available = True
        self.unavailable_reason = None
        self.checkpoints = [checkpoint]
        self._log_checkpoint()
        return {"status": "INITIALIZED", "checkpoint_available": True, "checkpoint": checkpoint.to_dict()}

    def register_change(self, change_set: dict[str, Any]) -> None:
        change_id = change_set["id"]
        self._registered[change_id] = change_set
        if change_id not in self.pending_changesets:
            self.pending_changesets.append(change_id)
        self._log_checkpoint()

    def update_change_verification(
        self,
        state: AgentState,
        change_id: str,
        verification_status: str,
    ) -> dict[str, Any]:
        allowed = {UNVERIFIED, VERIFIED, PARTIALLY_VERIFIED, REGRESSED}
        if verification_status not in allowed:
            return {"status": "INVALID_STATUS", "change_set_id": change_id, "reason": "unsupported verification status"}
        change = self._find_change(state, change_id)
        if not change:
            return {"status": "NOT_FOUND", "change_set_id": change_id}
        change["verification_status"] = verification_status
        return {"status": "UPDATED", "change_set_id": change_id, "verification_status": verification_status}

    def undo_changeset(self, state: AgentState, change_set_id: str, reason: str) -> dict[str, Any]:
        change = self._find_change(state, change_set_id)
        if not change:
            return {"status": "NOT_FOUND", "change_set_id": change_set_id}
        if change.get("apply_status") != APPLIED or change.get("rollback_status") != ROLLBACK_NONE:
            return {"status": UNSAFE_TO_UNDO, "change_set_id": change_set_id, "reason": "ChangeSet is not currently applied"}
        if change_set_id not in self.pending_changesets:
            return {
                "status": UNSAFE_TO_UNDO,
                "change_set_id": change_set_id,
                "reason": "ChangeSet is already part of a stable checkpoint or is no longer pending",
            }

        later_ids = self._pending_after(change_set_id)
        later_same_file = [item for item in later_ids if self._registered[item]["file"] == change["file"]]
        if later_same_file:
            return {
                "status": UNSAFE_TO_UNDO,
                "change_set_id": change_set_id,
                "reason": "a later ChangeSet modified the same file",
                "covering_changesets": later_same_file,
            }

        target = (self.workspace / change["file"]).resolve()
        try:
            target.relative_to(self.workspace)
            source = target.read_text(encoding="utf-8")
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            return {"status": UNSAFE_TO_UNDO, "change_set_id": change_set_id, "reason": str(exc)}

        start = int(change["start"])
        after = change["after"]
        before_context = change.get("context_before", "")
        after_context = change.get("context_after", "")
        target_matches = source[start:start + len(after)] == after
        prefix_matches = source[max(0, start - len(before_context)):start] == before_context
        suffix_start = start + len(after)
        suffix_matches = source[suffix_start:suffix_start + len(after_context)] == after_context
        if not (target_matches and prefix_matches and suffix_matches):
            return {
                "status": UNSAFE_TO_UNDO,
                "change_set_id": change_set_id,
                "reason": "current file no longer matches the ChangeSet output and local context",
            }

        try:
            restored = source[:start] + change["before"] + source[start + len(after):]
            target.write_text(restored, encoding="utf-8")
        except OSError as exc:
            return {"status": "FAILED", "change_set_id": change_set_id, "reason": str(exc)}

        change["rollback_status"] = UNDONE
        if change_set_id in self.pending_changesets:
            self.pending_changesets.remove(change_set_id)
        state.add_relevant_file(change["file"])
        if change.get("symbol"):
            state.add_relevant_symbol(change["symbol"])
        state.failed_attempts.append({"attempt": change_set_id, "reason": reason})
        state.add_action({
            "recovery": "CHANGESET_UNDO",
            "change_set_id": change_set_id,
            "status": UNDONE,
            "reason": reason,
        })
        result = {"status": UNDONE, "change_set_id": change_set_id, "file": change["file"], "reason": reason}
        self._log_recovery("CHANGESET_UNDO", result)
        return result

    def mark_stable(
        self,
        state: AgentState,
        reason: str,
        verification_ref: str | None = None,
    ) -> dict[str, Any]:
        unavailable = self._availability_result()
        if unavailable:
            return unavailable
        if not self.pending_changesets:
            return {"status": "NO_PENDING_CHANGES", "checkpoint": self.get_current_checkpoint()}
        if not self._head_matches_current():
            return {"status": UNEXPECTED_WORKSPACE_CHANGE, "reason": "HEAD changed outside CheckpointManager"}
        unknown = self._unknown_workspace_changes()
        if unknown:
            return {"status": UNEXPECTED_WORKSPACE_CHANGE, "unknown_paths": unknown}

        pending = [self._registered[item] for item in self.pending_changesets]
        unsupported = [
            item["id"] for item in pending
            if item.get("verification_status") not in {VERIFIED, PARTIALLY_VERIFIED}
            or item.get("rollback_status") != ROLLBACK_NONE
        ]
        if unsupported:
            return {
                "status": "VERIFICATION_REQUIRED",
                "reason": "stable checkpoints cannot contain unverified, regressed, or undone ChangeSets",
                "changesets": unsupported,
            }

        files = sorted({item["file"] for item in pending})
        added = self._git(["add", "--", *files], check=False)
        if added.returncode != 0:
            return {"status": "FAILED", "reason": added.stderr.strip()}
        number = len(self.checkpoints)
        committed = self._git(["commit", "-m", f"agent-checkpoint: stable state {number:03d}"], check=False)
        if committed.returncode != 0:
            self._git(["reset", "--", *files], check=False)
            return {"status": "FAILED", "reason": committed.stderr.strip() or committed.stdout.strip()}

        checkpoint = StableCheckpoint(
            id=f"checkpoint_{number:03d}",
            commit_hash=self._head() or "",
            reason=reason,
            created_step=state.current_step,
            linked_changesets=list(self.pending_changesets),
            verification_ref=verification_ref,
        )
        self.checkpoints.append(checkpoint)
        self.pending_changesets.clear()
        state.current_checkpoint = checkpoint.to_dict()
        print("\n[Checkpoint]", flush=True)
        print(f"Created: {checkpoint.id}", flush=True)
        print(f"Commit: {checkpoint.commit_hash}", flush=True)
        print(f"Changes: {', '.join(checkpoint.linked_changesets)}", flush=True)
        print(f"Reason: {reason}", flush=True)
        return {"status": "CREATED", "checkpoint": checkpoint.to_dict()}

    def rollback_last_stable(self, state: AgentState, reason: str) -> dict[str, Any]:
        unavailable = self._availability_result()
        if unavailable:
            return unavailable
        checkpoint = self.checkpoints[-1]
        if not self._head_matches_current():
            return {"status": UNEXPECTED_WORKSPACE_CHANGE, "reason": "HEAD changed outside CheckpointManager"}
        unknown = self._unknown_workspace_changes()
        if unknown:
            return {"status": UNEXPECTED_WORKSPACE_CHANGE, "unknown_paths": unknown}

        rolled_back = list(self.pending_changesets)
        reset = self._git(["reset", "--hard", checkpoint.commit_hash], check=False)
        if reset.returncode != 0:
            return {"status": "FAILED", "reason": reset.stderr.strip()}
        untracked_known = [path for path in self._known_pending_files() if (self.workspace / path).exists()]
        if untracked_known:
            self._git(["clean", "-f", "--", *untracked_known], check=False)

        for change_id in rolled_back:
            change = self._registered[change_id]
            change["rollback_status"] = CHECKPOINT_ROLLED_BACK
        self.pending_changesets.clear()
        state.current_checkpoint = checkpoint.to_dict()
        state.failed_attempts.append({"attempt": "stable checkpoint rollback", "reason": reason})
        state.add_action({
            "recovery": "STABLE_CHECKPOINT",
            "checkpoint": checkpoint.id,
            "rolled_back_changesets": rolled_back,
            "reason": reason,
        })
        for change_id in rolled_back:
            state.add_relevant_file(self._registered[change_id]["file"])
            if self._registered[change_id].get("symbol"):
                state.add_relevant_symbol(self._registered[change_id]["symbol"])
        result = {
            "status": "ROLLED_BACK",
            "checkpoint": checkpoint.to_dict(),
            "rolled_back_changesets": rolled_back,
            "reason": reason,
        }
        self._log_recovery("STABLE_CHECKPOINT", result)
        return result

    def can_rollback(self) -> bool:
        return bool(
            self.checkpoint_available
            and self.checkpoints
            and self.pending_changesets
            and self._head_matches_current()
            and not self._unknown_workspace_changes()
        )

    def get_current_checkpoint(self) -> dict[str, Any] | None:
        return self.checkpoints[-1].to_dict() if self.checkpoints else None

    def _pending_after(self, change_set_id: str) -> list[str]:
        try:
            index = self.pending_changesets.index(change_set_id)
        except ValueError:
            return []
        return self.pending_changesets[index + 1:]

    def _known_pending_files(self) -> set[str]:
        return {self._registered[item]["file"] for item in self.pending_changesets}

    def _unknown_workspace_changes(self) -> list[str]:
        known = self._known_pending_files()
        return sorted(path for path in self._actionable_status_paths() if path not in known)

    def _actionable_status_paths(self) -> set[str]:
        return {path for path in self._status_paths() if not self._is_known_generated_artifact(path)}

    @staticmethod
    def _is_known_generated_artifact(path: str) -> bool:
        """Recognize only narrow, reproducible Python runtime artifacts."""
        normalized = path.replace("\\", "/").strip("/")
        parts = normalized.split("/") if normalized else []
        return (
            "__pycache__" in parts
            or ".pytest_cache" in parts
            or normalized.lower().endswith(".pyc")
        )

    def _status_paths(self) -> set[str]:
        status = self._git(["-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all"], check=False)
        paths = set()
        for line in status.stdout.splitlines():
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            paths.add(path.replace("\\", "/"))
        return paths

    def _head_matches_current(self) -> bool:
        current = self.get_current_checkpoint()
        return bool(current and self._head() == current["commit_hash"])

    def _head(self) -> str | None:
        result = self._git(["rev-parse", "HEAD"], check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def _availability_result(self) -> dict[str, Any] | None:
        if self.checkpoint_available:
            return None
        return {
            "status": CHECKPOINT_UNAVAILABLE,
            "checkpoint_available": False,
            "reason": self.unavailable_reason,
        }

    def _unavailable(self, reason: str, status: str = CHECKPOINT_UNAVAILABLE) -> dict[str, Any]:
        self.checkpoint_available = False
        self.unavailable_reason = reason
        return {"status": status, "checkpoint_available": False, "reason": reason}

    def _git(self, arguments: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        command = ["git", "-C", str(self.workspace), *arguments]
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=60,
                check=check,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 127, "", f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _find_change(state: AgentState, change_id: str) -> dict[str, Any] | None:
        return next((item for item in state.change_sets if item["id"] == change_id), None)

    def _log_checkpoint(self) -> None:
        checkpoint = self.get_current_checkpoint()
        print("\n[Checkpoint]", flush=True)
        print(f"Current: {checkpoint['id'] if checkpoint else 'unavailable'}", flush=True)
        if checkpoint:
            print(f"Base commit: {checkpoint['commit_hash']}", flush=True)
        print(f"Pending changes: {len(self.pending_changesets)}", flush=True)

    @staticmethod
    def _log_recovery(kind: str, result: dict[str, Any]) -> None:
        print("\n[Recovery]", flush=True)
        print(f"Type: {kind}", flush=True)
        if result.get("change_set_id"):
            print(f"ChangeSet: {result['change_set_id']}", flush=True)
        if result.get("checkpoint"):
            print("From: working tree", flush=True)
            print(f"To: {result['checkpoint']['id']}", flush=True)
        if result.get("rolled_back_changesets"):
            print("Rolled back changes:", flush=True)
            for change_id in result["rolled_back_changesets"]:
                print(f"- {change_id}", flush=True)
        print(f"Status: {result['status']}", flush=True)
        print(f"Reason: {result.get('reason', '')}", flush=True)
