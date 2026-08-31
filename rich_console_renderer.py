"""Rich presentation layer for CodeAgent demos.

This module is intentionally read-only with respect to AgentState. It formats real
state, events, tool results, verification results, and recovery results produced by
the core modules; it never chooses an action or changes lifecycle state.
"""

from __future__ import annotations

import sys
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class RichConsoleRenderer:
    def __init__(self, console: Console | None = None):
        self.console = console or Console(file=sys.__stdout__, highlight=False)

    def header(self, state: Any) -> None:
        action_steps = [step for step in state.execution_plan if step.step_kind != "VERIFY"]
        total = len(action_steps)
        action_ids = {step.step_id for step in action_steps}
        completed = len(action_ids.intersection(state.completed_steps))
        verification = (state.verification_result or {}).get("overall_status", "PENDING")
        current = state.current_execution_step()
        # The core briefly remains in EXECUTING after the last implementation
        # step advances the cursor to a VERIFY step. VERIFYING is rendered on
        # the immediately following phase transition, so suppress this duplicate
        # presentation-only snapshot without changing AgentState.
        if state.current_phase == "EXECUTING" and current is not None and current.step_kind == "VERIFY":
            return
        if state.current_phase == "PLANNING":
            step_number = 0
            step_label = "未开始"
        elif state.current_phase == "DONE":
            step_number = total
            step_label = "已完成"
        elif state.current_phase == "VERIFYING" or (current is not None and current.step_kind == "VERIFY"):
            step_number = total
            step_label = "修改完成"
        elif current is not None and current.step_id in action_ids:
            step_number = next(
                index for index, item in enumerate(action_steps, 1)
                if item.step_id == current.step_id
            )
            step_label = current.step_id
        else:
            step_number = min(completed, total)
            step_label = "等待迁移"
        body = Text()
        body.append("阶段: ", style="dim")
        body.append(state.current_phase, style="bold blue")
        body.append(f"      执行: {step_number}/{total} · {step_label}", style="white")
        body.append("      验证: ", style="dim")
        body.append(verification, style=self._status_style(verification))
        self.console.print(Panel(body, title="Coding Agent", border_style="blue", expand=True))

    def planning(self, state: Any) -> None:
        self.console.print("\n[bold blue]验收标准[/bold blue]")
        for criterion in state.acceptance_criteria:
            self.console.print(
                Text.assemble(
                    ("✓ ", "green"),
                    (criterion.id, "bold"),
                    (f" [{criterion.criticality}][{criterion.evidence_type}] ", "cyan"),
                    criterion.description,
                )
            )

        self.console.print("\n[bold blue]执行计划[/bold blue]")
        action_count = 0
        verification_count = 0
        for index, step in enumerate(state.execution_plan, 1):
            if step.step_kind == "VERIFY":
                verification_count += 1
                kind_style = "yellow"
            else:
                action_count += 1
                kind_style = "cyan"
            self.console.print(
                Text.assemble(
                    (f"{index}. ", "dim"),
                    (step.step_id, "bold"),
                    (f" [{step.step_kind}] ", kind_style),
                    step.description,
                )
            )
        self.console.print(
            f"[dim]实施步骤: {action_count} · 验证步骤: {verification_count}；"
            "顶部“执行”进度只统计实施步骤。[/dim]"
        )

        self.console.print("\n[green]✓[/green] Verification Contract [bold]已冻结[/bold]")
        self.console.print("[dim]最小输入与断言均已在修改前固定；hidden evaluator 不在 workspace 中。[/dim]")

    def baseline_item(self, check: Any, result: dict[str, Any]) -> None:
        passed = self._passed(result)
        icon = "✓" if passed else "✗"
        style = "green" if passed else "red"
        self.console.print(
            Text.assemble(
                (f"{icon} ", style),
                (f"{check.evidence_type.title():10}", "cyan"),
                (f" · {check.description}", "white"),
                (f"  {'PASS' if passed else 'FAIL'}", style),
            )
        )

    def baseline_start(self) -> None:
        self.console.print("\n[bold blue][Baseline][/bold blue]")

    def structured_edit(self, result: dict[str, Any]) -> None:
        change = result.get("change_set") or {}
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", width=10)
        table.add_column()
        table.add_row("文件", str(result.get("file", "")))
        table.add_row("Symbol", str(result.get("symbol") or "—"))
        table.add_row("Intent", str(result.get("intent", "")))
        self.console.print(Panel(table, title="结构化编辑", border_style="blue", expand=False))
        resolution = result.get("resolution") or "UNKNOWN"
        self.console.print(f"Resolver  [green]✓[/green] {resolution}")
        self.console.print(f"ChangeSet [bold]{change.get('id', '—')}[/bold]  [green]✓ APPLIED[/green]")
        self.console.print(f"Verification: [yellow]{change.get('verification_status', 'UNVERIFIED')}[/yellow]")
        preview = self._change_preview(change.get("after", ""))
        if preview:
            self.console.print(f"[dim]关键修改: {preview}[/dim]")

    def verification_start(self) -> None:
        self.console.print("\n[bold blue][验证][/bold blue]")

    def verification_item(
        self,
        check: Any,
        result: dict[str, Any],
        baseline: dict[str, Any] | None,
    ) -> None:
        passed = self._passed(result)
        before = self._passed(baseline or {})
        icon = "✓" if passed else "✗"
        style = "green" if passed else "red"
        if check.evidence_type == "SANITY":
            delta = "PASS" if passed else "FAIL"
        else:
            delta = f"{'PASS' if before else 'FAIL'} → {'PASS' if passed else 'FAIL'}"
        self.console.print(
            Text.assemble(
                (f"{icon} ", style),
                (check.evidence_type, "bold cyan"),
                (f" · {check.description:<34} ", "white"),
                (delta, style),
            )
        )

    def verification_summary(self, result: dict[str, Any]) -> None:
        overall = result.get("overall_status", "UNVERIFIED")
        self.console.rule(style="dim")
        self.console.print(
            Text.assemble(
                ("Overall: ", "bold"),
                (overall, f"bold {self._status_style(overall)}"),
                (f"  ({result.get('overall_reason', '')})", "dim"),
            )
        )

    def recovery(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        self.console.print("\n[bold yellow][恢复][/bold yellow]")
        status = result.get("status", "UNKNOWN")
        if result.get("change_set_id"):
            self.console.print(
                f"[green]✓[/green] ChangeSet [bold]{result['change_set_id']}[/bold] {status}"
            )
        else:
            checkpoint = (result.get("checkpoint") or {}).get("id", "—")
            self.console.print(f"[green]✓[/green] Stable Checkpoint {checkpoint} {status}")

    def debugging_evidence(
        self,
        failure: dict[str, Any] | None,
        check_id: str,
        baseline: dict[str, Any] | None,
        current: dict[str, Any] | None,
    ) -> None:
        """Render selected real failure evidence without interpreting lifecycle state."""
        failure = failure or {}
        baseline = baseline or {}
        current = current or {}
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim", width=14)
        table.add_column()
        table.add_row("FailureEvent", f"{failure.get('id', '—')} · {failure.get('type', '—')}")
        table.add_row("Fingerprint", str(failure.get("fingerprint") or "—"))
        table.add_row("失败检查", check_id)
        table.add_row("Baseline", self._observation_summary(baseline))
        table.add_row("Current", self._observation_summary(current))
        table.add_row("相关 ChangeSet", str(failure.get("related_changeset") or "—"))
        self.console.print(Panel(table, title="DEBUGGING 证据", border_style="red", expand=False))

    def transition(self, transition: Any) -> None:
        if transition.previous_phase == transition.next_phase and transition.event == "EDIT_APPLIED":
            return
        self.console.print("\n[bold blue]状态迁移[/bold blue]")
        self.console.print(
            f"{transition.previous_phase} [dim]-- {transition.event} -->[/dim] "
            f"[bold]{transition.next_phase}[/bold]"
        )

    def checkpoint(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        checkpoint = result.get("checkpoint") or {}
        if checkpoint.get("id"):
            self.console.print(f"\n[green]✓[/green] Stable checkpoint: [bold]{checkpoint['id']}[/bold]")

    def final(self, state: Any, hidden: Any) -> None:
        verification = (state.verification_result or {}).get("overall_status", "UNVERIFIED")
        table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
        table.add_column(style="bold")
        table.add_column()
        table.add_row("Agent Result:", Text(verification, style=self._status_style(verification)))
        table.add_row("Hidden Evaluation:", Text("PASS" if hidden.success else "FAIL", style="green" if hidden.success else "red"))
        false_success = state.current_phase == "DONE" and not hidden.success
        table.add_row("False Success:", Text("Yes" if false_success else "No", style="red" if false_success else "green"))
        self.console.print("\n")
        self.console.print(table)

    @staticmethod
    def _passed(result: dict[str, Any]) -> bool:
        return result.get("status") == "SUCCESS" and result.get("exit_code") == 0

    @staticmethod
    def _status_style(status: str) -> str:
        return {
            "VERIFIED": "green", "PASS": "green", "DONE": "green",
            "REGRESSED": "red", "FAILED": "red", "FAIL": "red",
            "UNVERIFIED": "yellow", "PENDING": "yellow",
        }.get(status, "white")

    @staticmethod
    def _change_preview(after: str) -> str:
        lines = [line.strip() for line in after.splitlines() if line.strip()]
        interesting = [
            line for line in lines
            if "except " in line or "--min-age" in line or "if " in line or "continue" in line
        ]
        selected = interesting[:2] or lines[:2]
        text = " | ".join(selected)
        return text if len(text) <= 120 else text[:117] + "..."

    @staticmethod
    def _observation_summary(observation: dict[str, Any]) -> str:
        passed = RichConsoleRenderer._passed(observation)
        stdout = str(observation.get("stdout") or "").strip()
        value = stdout.splitlines()[-1] if stdout else "(无输出)"
        return f"{'PASS' if passed else 'FAIL'} · stdout: {value}"
