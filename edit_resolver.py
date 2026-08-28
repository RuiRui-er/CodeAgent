"""Deterministic resolver for one local structured edit at a time."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from edit_models import (
    AMBIGUOUS_TARGET,
    INVALID_EDIT,
    STALE_EDIT,
    TARGET_NOT_FOUND,
    EditCandidate,
    ResolvedEdit,
    StructuredEditRequest,
)


MAX_EDIT_LINES = 120
MAX_CANDIDATES = 8
ANCHOR_CONTEXT_LINES = 40
CANDIDATE_CONTEXT_LINES = 4


@dataclass(frozen=True)
class SymbolScope:
    name: str
    start: int
    end: int


class EditResolver:
    def __init__(self) -> None:
        self.pending_request: StructuredEditRequest | None = None
        self.pending_candidates: dict[str, EditCandidate] = {}

    def resolve(self, request: StructuredEditRequest, source: str, path: Path) -> ResolvedEdit | dict[str, Any]:
        self.pending_request = None
        self.pending_candidates = {}
        invalid = self._validate(request, source)
        if invalid:
            return invalid

        scopes = self._symbol_scopes(source, path.suffix.lower())
        if request.operation == "insert":
            return self._resolve_insert(request, source, scopes)

        old_block = request.old_block or ""
        if request.symbol:
            scope = next((item for item in scopes if item.name == request.symbol), None)
            if scope:
                matches = self._matches(source, old_block, scope.start, scope.end)
                if len(matches) == 1:
                    return self._resolved(request, source, matches[0], "SYMBOL_SCOPE", scope.name)
                if len(matches) > 1:
                    return self._ambiguous(request, source, path, matches, scopes)
                return self._stale(request, source[scope.start:scope.end], "old_block no longer matches the current symbol source")

        if request.anchor:
            anchor_positions = self._matches(source, request.anchor, 0, len(source))
            if anchor_positions:
                local_matches: set[tuple[int, int]] = set()
                for start, _ in anchor_positions:
                    window_start, window_end = self._line_window(source, start, ANCHOR_CONTEXT_LINES)
                    local_matches.update(self._matches(source, old_block, window_start, window_end))
                matches = sorted(local_matches)
                if len(matches) == 1:
                    symbol = self._containing_symbol(matches[0][0], scopes)
                    return self._resolved(request, source, matches[0], "ANCHOR_LOCAL_CONTEXT", symbol)
                if len(matches) > 1:
                    return self._ambiguous(request, source, path, matches, scopes)
                window_start, window_end = self._line_window(source, anchor_positions[0][0], ANCHOR_CONTEXT_LINES)
                return self._stale(request, source[window_start:window_end], "old_block no longer matches near the current anchor")

        matches = self._matches(source, old_block, 0, len(source))
        if len(matches) == 1:
            symbol = self._containing_symbol(matches[0][0], scopes)
            return self._resolved(request, source, matches[0], "UNIQUE_CONTENT", symbol)
        if len(matches) > 1:
            return self._ambiguous(request, source, path, matches, scopes)
        return {
            "status": TARGET_NOT_FOUND,
            "file": request.file,
            "symbol": request.symbol,
            "anchor": request.anchor,
            "reason": "old_block was not found in the current file",
        }

    def resolve_candidate(self, candidate_id: str, source: str) -> ResolvedEdit | dict[str, Any]:
        request = self.pending_request
        candidate = self.pending_candidates.get(candidate_id)
        if not request or not candidate:
            return {
                "status": INVALID_EDIT,
                "candidate_id": candidate_id,
                "reason": "candidate_id is unknown or no ambiguous edit is pending",
            }
        expected = request.anchor if request.operation == "insert" else request.old_block
        if source[candidate.start:candidate.end] != (expected or ""):
            return self._stale(request, self._context(source, candidate.start, candidate.end), "candidate source changed after disambiguation")
        self.pending_request = None
        self.pending_candidates = {}
        if request.operation == "insert":
            return ResolvedEdit(
                request.file,
                request.operation,
                request.intent,
                candidate.symbol,
                candidate.end,
                candidate.end,
                "",
                request.new_block or "",
                "CANDIDATE_SELECTION",
            )
        return self._resolved(request, source, (candidate.start, candidate.end), "CANDIDATE_SELECTION", candidate.symbol)

    def _resolve_insert(
        self,
        request: StructuredEditRequest,
        source: str,
        scopes: list[SymbolScope],
    ) -> ResolvedEdit | dict[str, Any]:
        if not request.anchor:
            return {
                "status": INVALID_EDIT,
                "file": request.file,
                "reason": "insert requires an anchor in this conservative first version",
            }
        search_start, search_end = 0, len(source)
        symbol_name = request.symbol
        if symbol_name:
            scope = next((item for item in scopes if item.name == symbol_name), None)
            if scope:
                search_start, search_end = scope.start, scope.end
            else:
                symbol_name = None
        matches = self._matches(source, request.anchor, search_start, search_end)
        if len(matches) == 1:
            position = matches[0][1]
            return ResolvedEdit(
                file=request.file,
                operation=request.operation,
                intent=request.intent,
                symbol=symbol_name or self._containing_symbol(position, scopes),
                start=position,
                end=position,
                before="",
                after=request.new_block or "",
                resolution="SYMBOL_ANCHOR" if symbol_name else "UNIQUE_ANCHOR",
            )
        if len(matches) > 1:
            return self._ambiguous(request, source, Path(request.file), matches, scopes)
        if request.symbol and symbol_name:
            scope = next(item for item in scopes if item.name == symbol_name)
            return self._stale(request, source[scope.start:scope.end], "anchor no longer matches the current symbol source")
        return {"status": TARGET_NOT_FOUND, "file": request.file, "reason": "anchor was not found"}

    def _validate(self, request: StructuredEditRequest, source: str) -> dict[str, Any] | None:
        if request.operation not in {"replace", "insert", "delete"}:
            return {"status": INVALID_EDIT, "file": request.file, "reason": "operation must be replace, insert, or delete"}
        if request.operation in {"replace", "delete"} and not request.old_block:
            return {"status": INVALID_EDIT, "file": request.file, "reason": f"{request.operation} requires old_block"}
        if request.operation in {"replace", "insert"} and request.new_block is None:
            return {"status": INVALID_EDIT, "file": request.file, "reason": f"{request.operation} requires new_block"}
        old_lines = (request.old_block or "").count("\n") + 1
        new_lines = (request.new_block or "").count("\n") + 1
        if max(old_lines, new_lines) > MAX_EDIT_LINES:
            return {"status": INVALID_EDIT, "file": request.file, "reason": f"edit exceeds MAX_EDIT_LINES ({MAX_EDIT_LINES})"}
        if request.old_block == source:
            return {"status": INVALID_EDIT, "file": request.file, "reason": "whole-file replacement is not allowed"}
        return None

    def _resolved(
        self,
        request: StructuredEditRequest,
        source: str,
        match: tuple[int, int],
        resolution: str,
        symbol: str | None,
    ) -> ResolvedEdit:
        start, end = match
        after = "" if request.operation == "delete" else (request.new_block or "")
        return ResolvedEdit(request.file, request.operation, request.intent, symbol, start, end, source[start:end], after, resolution)

    def _ambiguous(
        self,
        request: StructuredEditRequest,
        source: str,
        path: Path,
        matches: list[tuple[int, int]],
        scopes: list[SymbolScope],
    ) -> dict[str, Any]:
        if len(matches) > MAX_CANDIDATES:
            self.pending_request = None
            self.pending_candidates = {}
            return {
                "status": AMBIGUOUS_TARGET,
                "file": request.file,
                "candidate_count": len(matches),
                "candidates": [],
                "reason": "too many candidates; provide a more specific symbol or anchor",
            }
        candidates: list[EditCandidate] = []
        for index, (start, end) in enumerate(matches):
            candidate = EditCandidate(
                id=chr(ord("A") + index),
                file=request.file,
                symbol=self._containing_symbol(start, scopes),
                start=start,
                end=end,
                line_range=self._line_range(source, start, end),
                context=self._context(source, start, end),
            )
            candidates.append(candidate)
        self.pending_request = request
        self.pending_candidates = {item.id: item for item in candidates}
        return {
            "status": AMBIGUOUS_TARGET,
            "file": request.file,
            "candidate_count": len(candidates),
            "candidates": [item.public() for item in candidates],
        }

    @staticmethod
    def _stale(request: StructuredEditRequest, current: str, reason: str) -> dict[str, Any]:
        return {
            "status": STALE_EDIT,
            "file": request.file,
            "symbol": request.symbol,
            "anchor": request.anchor,
            "reason": reason,
            "current_context": current[:3000],
        }

    @staticmethod
    def _matches(source: str, needle: str, start: int, end: int) -> list[tuple[int, int]]:
        matches = []
        position = source.find(needle, start, end)
        while position != -1:
            matches.append((position, position + len(needle)))
            position = source.find(needle, position + max(1, len(needle)), end)
        return matches

    @staticmethod
    def _line_offsets(source: str) -> list[int]:
        offsets = [0]
        offsets.extend(match.end() for match in re.finditer("\n", source))
        return offsets

    def _symbol_scopes(self, source: str, suffix: str) -> list[SymbolScope]:
        if suffix == ".py":
            try:
                tree = ast.parse(source)
                offsets = self._line_offsets(source)
                scopes: list[SymbolScope] = []

                def visit(nodes: list[ast.stmt], parents: list[str]) -> None:
                    for node in nodes:
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                            name = ".".join(parents + [node.name])
                            decorator_lines = [item.lineno for item in getattr(node, "decorator_list", [])]
                            start_line = min([node.lineno, *decorator_lines])
                            start = offsets[start_line - 1]
                            end_line = getattr(node, "end_lineno", node.lineno)
                            end = offsets[end_line] if end_line < len(offsets) else len(source)
                            scopes.append(SymbolScope(name, start, end))
                            visit(node.body, parents + [node.name])

                visit(tree.body, [])
                return scopes
            except SyntaxError:
                pass
        return self._fallback_scopes(source)

    def _fallback_scopes(self, source: str) -> list[SymbolScope]:
        pattern = re.compile(
            r"(?m)^\s*(?:(?:class|def|function|func)\s+(?P<decl>[A-Za-z_$][\w$]*)"
            r"|(?:(?:public|private|protected|static|async|virtual|final)\s+)*"
            r"[A-Za-z_$][\w$<>\[\],.?]*\s+(?P<signature>[A-Za-z_$][\w$]*)\s*\([^;{}]*\)\s*\{)"
        )
        matches = list(pattern.finditer(source))
        scopes = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
            scopes.append(SymbolScope(match.group("decl") or match.group("signature"), match.start(), end))
        return scopes

    @staticmethod
    def _containing_symbol(position: int, scopes: list[SymbolScope]) -> str | None:
        containing = [scope for scope in scopes if scope.start <= position < scope.end]
        return min(containing, key=lambda item: item.end - item.start).name if containing else None

    def _line_window(self, source: str, position: int, radius: int) -> tuple[int, int]:
        offsets = self._line_offsets(source)
        line = max(0, sum(1 for offset in offsets if offset <= position) - 1)
        start_line = max(0, line - radius)
        end_line = min(len(offsets) - 1, line + radius + 1)
        start = offsets[start_line]
        end = offsets[end_line] if end_line < len(offsets) else len(source)
        return start, end

    def _context(self, source: str, start: int, end: int) -> str:
        window_start, window_end = self._line_window(source, start, CANDIDATE_CONTEXT_LINES)
        return source[window_start:max(window_end, end)]

    @staticmethod
    def _line_range(source: str, start: int, end: int) -> str:
        start_line = source.count("\n", 0, start) + 1
        end_line = source.count("\n", 0, end) + 1
        return f"{start_line}-{end_line}"
