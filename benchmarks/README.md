# Benchmark & Evaluation Harness

This directory evaluates the existing Coding Agent without changing its core
planning, context, editing, verification, recovery, or orchestration behavior.

## Isolation model

Each task has three parts:

- `task.json`: public task description and benchmark metadata.
- `repo/`: the initial repository copied into a fresh run workspace.
- `hidden_tests/`: ground-truth checks that are never copied into the Agent workspace.

Before every run, the runner copies `repo/`, initializes an independent Git
repository, creates a commit with fixed author/committer metadata, verifies the
expected commit hash, and checks that the workspace is clean. This reset does not
touch the CodeAgent repository history.

## Running

Use a real configured model endpoint for a full Agent run:

```powershell
python -m benchmarks.benchmark_runner --task bug_001 --variant full --runs 3
```

For an offline infrastructure smoke test, `--smoke-agent` exercises reset,
artifact capture, hidden evaluation, false-success computation, and summary
generation with an intentional no-op adapter. It does not claim Agent task
effectiveness.

```powershell
python -m benchmarks.benchmark_runner --task bug_001 --variant full --runs 1 --smoke-agent --save-demo-trace
```

Run artifacts are written under ignored `benchmarks/runs/` directories:
`trajectory.json`, `agent_log.txt`, `final_state.json`, `result.json`, optional
`failure_summary.json`, and optional `trace_summary.txt`. Aggregate JSON and CSV
summaries are emitted alongside the run directories.

## Variants and metrics

`full` is the only executable configuration in this phase. The variant table
reserves simple flags for full-history/recent-N context, exact replacement,
evidence-gate removal, and failure-memory removal. Reserved variants fail fast;
they do not silently modify Agent behavior.

Results include hidden success and false success, LLM calls and context sizes,
repeated actions, failure/regression counts, undo/rollback/replan counts, edit
rejections, final checkpoint, termination reason, and hidden evaluator evidence.
Wrong-location edit count is explicitly `null` until it can be measured reliably.
Failure summaries are factual extractions only and never ask an LLM for a cause.
