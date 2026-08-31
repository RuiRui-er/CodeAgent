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
        completed = len(state.completed_steps)
        total = len(state.execution_plan)
        verification = (state.verification_result or {}).get("overall_status", "PENDING")
        current = state.current_execution_step()
        if (
            current and current.step_kind == "VERIFY" and verification == "VERIFIED"
            and current.step_id not in state.completed_steps
        ):
            completed += 1
        body = Text()
        body.append("阶段: ", style="dim")
        body.append(state.current_phase, style="bold blue")
        body.append(f"      计划: {completed}/{total}", style="white")
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

    def transition(self, transition: Any) -> None:
        if transition.previous_phase == transition.next_phase and transition.event == "EDIT_APPLIED":
            return
        self.console.print("\n[bold blue]状态迁移[/bold blue]")
        self.console.print(
            f"{transition.previous_phase} [dim]-- {transition.event} -->[/dim] "
            f"[bold]{transition.next_phase}[/bold]"
        )

    def decision_summary(self, current_step: str, evidence: str, next_action: str) -> None:
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("当前步骤", current_step)
        table.add_row("依据", evidence)
        table.add_row("下一动作", next_action)
        self.console.print(Panel(table, title="决策摘要", border_style="yellow", expand=False))

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
