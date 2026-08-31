# Minimal Coding Agent Baseline

这是一个刻意保持简单的 Coding Agent。它不使用 LangChain、OpenAI Agents SDK、
AutoGen 等 Agent 框架或 SDK；Agent 循环、消息历史、工具定义与执行、结果回传、
终止条件和错误处理均自行实现。状态数据与上下文选择分别拆分在
`agent_state.py` 和 `context_manager.py`；工具注册、安全策略与执行分别拆分在
`tool_registry.py`、`tool_safety.py` 和 `tool_executor.py`，入口循环仍位于
`coding_agent.py`。

## 运行环境

- Python 3.10 或更高版本
- 一个支持 OpenAI Chat Completions 与原生 tool calling 的模型服务
- 无第三方 Python 依赖

先通过系统环境变量提供凭据和模型配置（不要写入项目文件）：

```text
OPENAI_API_KEY      必填
OPENAI_MODEL        可选，默认 gpt-4o-mini
OPENAI_BASE_URL     可选，默认 https://api.openai.com/v1
```

运行：

```powershell
python coding_agent.py --workspace demo_project "阅读项目，定位并修复测试暴露的 bug，然后运行测试验证"
```

也可以省略任务文本，程序会交互式询问。使用 `--max-steps 8` 可调整最大模型轮数。

## Agent 闭环

1. 进入 `PLANNING`，模型只使用浏览、读取、搜索和命令工具理解项目；
2. 模型通过 `submit_plan` 生成结构化验收标准、验证契约和执行计划；
3. 程序在修改前执行契约中标记的 baseline 检查并保存结果；
4. 完整打印规划状态，然后进入原有执行循环；
5. 模型使用当前 phase 允许的工具实施计划；调用 `finish` 后只进入 `VERIFYING`，
   不会直接把任务标记为完成。

程序会逐步打印模型消息、工具名、参数、执行结果和终止原因。每次调用模型前还会打印
`[Context]`，列出当前 phase、实际包含的 section 和总字符数。

规划结果保存在 `AgentState`，包含 `original_task`、`task_understanding`、
`acceptance_criteria`、`verification_contract`、`baseline`、`execution_plan` 和
`current_step`。验收标准和验证契约在进入执行阶段前确定，执行提示明确禁止模型为了
适配实现而削弱它们。本阶段只区分 planning 与 execution，没有引入完整状态机。

`--max-planning-steps 8` 可单独限制规划轮数。若模型在限制内没有提交有效结构化计划，
程序会停止而不会进入执行阶段。

## State-aware Context Management

程序不会把持续增长的完整 `messages` 原样发送给模型。完整 tool/assistant trajectory 在
当前运行中保留用于日志记录；每次模型请求则由 `ContextManager` 根据 `current_phase`
重新构造：

- `PLANNING`：任务、验收标准、规划发现和少量最近操作；
- `EXECUTING`：任务、当前步骤、步骤关联验收标准、最新相关代码、最近操作和失败尝试；
- `VERIFYING`：验收标准、验证契约、baseline、修改摘要、已完成步骤和最新结果；
- `DEBUGGING`：任务、当前步骤、失败证据、失败尝试、最新相关代码和最近操作。

各 phase 使用 `PHASE_SECTION_BUDGETS` 中固定、可读的 section 字符上限，并受
`MAX_CONTEXT_CHARS` 总上限约束；不依赖 tokenizer。`recent_actions` 固定只保留最近
5 项。重要事实、完成步骤和失败尝试分别增量写入 `confirmed_facts`、
`completed_steps`、`failed_attempts`，不调用模型生成历史摘要。

`relevant_files` 只保存相对路径，`relevant_symbols` 只保存符号名。构造 EXECUTING 或
DEBUGGING 上下文时，ContextManager 会从 workspace 重新读取文件并按符号截取附近代码，
因此磁盘上的当前文件始终是代码内容的唯一真实来源。

## Confidence-aware Structured Editing

`apply_patch` 不再接收整文件内容或 unified diff，而是接收单个局部修改意图：

```json
{
  "file": "src/parser.py",
  "operation": "replace",
  "intent": "Handle empty input",
  "symbol": "Parser.parse",
  "anchor": "def parse(self, value):",
  "old_block": "return None",
  "new_block": "return ''"
}
```

`operation` 支持 `replace`、`insert`、`delete`。replace/delete 必须包含 `old_block`，
replace/insert 必须包含 `new_block`；保守起见，第一版 insert 还要求稳定的 `anchor`。
一次请求只能修改一个文件中的一个局部位置，且受可配置的 `MAX_EDIT_LINES` 限制。

`EditResolver` 每次从磁盘当前内容开始，依次尝试 Python AST symbol scope、anchor 附近
local context、whole-file unique content，只有唯一定位才应用。如果存在多个精确匹配，
返回带 symbol、行范围和上下文的候选列表；下一次可仅传 `candidate_id`。如果 symbol 或
anchor 仍存在但 `old_block` 已消失，则返回 `STALE_EDIT` 和当前局部代码，不猜测新位置。
所有定位都是离散规则，不使用 fuzzy score。

成功写回后记录轻量 ChangeSet：ID、文件、symbol、operation、intent、before、after、
当前 step、phase 和 checkpoint base。状态拆分为 `apply_status`、
`verification_status`、`rollback_status`；新 ChangeSet 固定从
`APPLIED / UNVERIFIED / NONE` 开始，不会因为写入成功就自动 VERIFIED。

## ChangeSet Recovery 与 Stable Checkpoint

`CheckpointManager` 维护两条语义分离的历史：ChangeSet 记录 Agent 做过的局部修改，
Stable Checkpoint 只记录已经由上层验证接口确认值得保存的 Git 版本。一次
`apply_patch` 只注册 pending ChangeSet，不创建 Git commit。

局部 `undo_changeset` 只处理仍 pending 的 ChangeSet。它拒绝撤销已进入 checkpoint 的
修改，也拒绝撤销已被后续同文件 ChangeSet 覆盖的修改。恢复前会再次核对保存的 offset、
after 内容及前后局部上下文；任何不一致都返回 `UNSAFE_TO_UNDO`，不会猜测。Undo 保留
原有 verification 结论，只把 `rollback_status` 更新为 `UNDONE`。

如果 target workspace 自身是 clean Git repo，启动时直接把当前 HEAD 登记为
`checkpoint_000`，不会创建额外 commit。如果 workspace 非 Git repo、只是嵌套在其他
仓库下，或 working tree 已有用户修改，checkpoint 功能不可用；不会自动 git init、
stash、commit 或 reset，但 ChangeSet Undo 仍可使用。

上层通过 `update_change_verification` 写入 `VERIFIED`、`PARTIALLY_VERIFIED` 或
`REGRESSED`。只有 pending ChangeSets 均具有可接受的验证状态时，显式调用
`mark_stable(reason, verification_ref)` 才会仅 stage Agent 已知文件并创建固定格式的
checkpoint commit。CheckpointManager 不运行测试，也不自行判断验证结果。

`rollback_last_stable` 会先比较当前 HEAD，并使用 pending ChangeSet 文件集合与
`git status --porcelain --untracked-files=all` 检测未知修改。存在用户手动产生的未知文件
或修改时返回 `UNEXPECTED_WORKSPACE_CHANGE`，禁止 reset。安全回滚后 pending
ChangeSets 标记为 `CHECKPOINT_ROLLED_BACK`，phase 切到 DEBUGGING，并记录恢复原因。

Checkpoint demo 始终在被忽略的独立嵌套仓库中运行，避免污染 CodeAgent 自身历史：

```text
CodeAgent/
    agent source...
    .demo_checkpoint_runtime/
        target_repo/
            .git/
            app.py
```

## 工具执行与安全边界

核心工具为 `read_file`、`list_dir`、`search_code`、`apply_patch`、`run_command`
和 `finish`。`write_file` 只保留底层代码兼容，不再暴露给模型；模型修改文件必须使用
由 EditResolver 唯一定位的结构化 `apply_patch`。工具按副作用分为：

- `READ_ONLY`：`read_file`、`list_dir`、`search_code`；
- `MUTATING`：`apply_patch`、兼容的 `write_file`；
- `EXECUTION`：`run_command`；
- `CONTROL`：`finish`。

`tool_registry.py` 集中维护 phase 权限。PLANNING 禁止修改文件，只允许读取工具和有限
测试/只读查询命令；EXECUTING 与 DEBUGGING 可修改、执行并请求 `finish`；VERIFYING
只允许读取、搜索和运行验证命令。未授权调用返回包含当前可用替代工具的 `BLOCKED`
结果，不会导致 Agent 崩溃。

`CommandPolicy` 使用离散的 `SAFE / CONFIRM / DENY`：常见测试、编译和只读 Git 命令
直接执行；删除、覆盖、安装依赖、未知或复杂命令需要同步确认；系统级危险命令和明显
workspace 路径逃逸直接拒绝，不能通过确认绕过。命令始终使用 workspace 作为 `cwd`、
`shell=False`、捕获标准输出与错误并限制为 1–120 秒。超过
`MAX_COMMAND_OUTPUT_CHARS` 时同时保留头尾，在中间标记省略的字符和行数。

`WorkspaceGuard` 集中处理文件路径，resolve 后必须仍位于 workspace 内。该实现只是
workspace-scoped best-effort policy，不是真正的 OS sandbox；面对不可信命令仍应使用
容器或受限系统账户。

## Demo

`demo_project` 中有一个很小的除法函数及失败测试。它故意把除数和被除数写反，
可用于演示 Agent 完成“查看文件 → 运行测试 → 定位问题 → 修改 → 再次测试”的闭环。

手工查看 demo 的初始失败状态：

```powershell
cd demo_project
python -m unittest -v
```

若要重复演示，请先把 `calculator.py` 中的返回表达式恢复成 `count / total`。

运行规划数据结构与 baseline 的离线单元测试（不调用模型 API）：

```powershell
python -m unittest -v test_planning.py
python -m unittest -v test_context_manager.py
python -m unittest -v test_tool_safety.py
python -m unittest -v test_edit_resolver.py
python -m unittest -v test_checkpoint_manager.py
```

离线观察四个 phase 的上下文组成（不调用模型 API，也不修改 demo 文件）：

```powershell
python demo_context.py
python demo_tool_safety.py
python demo_structured_edit.py
python demo_checkpoint_recovery.py
python demo_evidence_gated_verification.py
python demo_failure_aware_recovery.py
python demo_state_machine.py
```

## 状态机收口

`AgentOrchestrator` 是现有能力之间的协调层，不是新的 Agent 核心能力。ToolExecutor、
VerificationEngine、FailureRecovery 和 CheckpointManager 分别继续负责工具结果、证据判定、
失败决策和 Git 恢复；它们不再直接修改 lifecycle phase。主循环把结果转换成有限的
`AgentEvent`，由 Orchestrator 返回 `TransitionResult` 并唯一更新 `current_phase`。

保留的 phase 只有：`PLANNING / EXECUTING / VERIFYING / DEBUGGING / DONE / FAILED`。
没有 DECIDING、REPLANNING、VERIFIED_SUCCESS 或 WAITING_USER，也没有 EventBus、workflow
engine 或 graph framework。

主要事件包括：PLAN_READY、PLAN_BLOCKED_BY_USER_INTENT、TOOL_FAILED、EDIT_APPLIED、
EDIT_FAILED、FINISH_REQUESTED、VERIFICATION_REQUESTED、INCREMENTAL_VERIFIED、
INCREMENTAL_PARTIAL、FINAL_VERIFIED、FINAL_PARTIAL、VERIFICATION_REGRESSED、
TARGET_FAILED、VERIFICATION_UNVERIFIED、REPLAN_REQUIRED、MAX_STEPS_REACHED 和
UNRECOVERABLE_FAILURE。当前 CLI 明确禁用 HUMAN planning，因为尚无可恢复的人工确认通道。

`TransitionResult` 明确记录 previous phase、event、next phase、reason、是否暂停自动循环以及
是否需要用户确认。每次合法迁移还会向 `AgentState.phase_history` 追加 from/event/to/reason，
不保存 prompt。

Final 与 incremental verification 由事件明确区分：finish 只能产生 FINAL VERIFYING，final
VERIFIED/PARTIAL 才能进入 DONE；incremental VERIFIED/PARTIAL 返回 EXECUTING。REGRESSED
在 VerificationEngine 完成既有 recovery 后通过事件进入 DEBUGGING；Critical Target FAIL
同样进入 DEBUGGING。证据不足的 UNVERIFIED 保持 VERIFYING，设置
`needs_user_confirmation=true` 并暂停自动循环。

FailureRecovery 只返回 `CONTINUE_DEBUGGING` 或 `REPLAN_REQUIRED` decision。重复失败的
DEBUGGING → PLANNING 由 Orchestrator 完成。CheckpointManager rollback 也只返回恢复结果，
不再隐式切 phase。

MAX_AGENT_STEPS 从任意非 terminal phase 统一进入 FAILED，并生成 criteria、verification、
last failure、pending ChangeSets、stable checkpoint 和 phase history 摘要。DONE/FAILED 是
terminal phase，任何后续 transition 都会被拒绝。

状态机一致性检查和离线演示：

```powershell
python -m unittest -v test_agent_orchestrator.py
python demo_state_machine.py
```

## Evidence-Gated Verification

`finish` 现在只表示申请结束。`VerificationEngine` 会读取 PLANNING 阶段已经冻结的
Acceptance Criteria、Verification Contract 和 baseline，重新执行最终 AUTO checks；
它不会修改 criterion、expected output、command 或 baseline，也不会把模型代码审查当成
正式证据。

验证结果分为两层：单条 `CriterionResult` 使用 `PASS / FAIL / UNVERIFIED /
NOT_APPLICABLE`，记录 criterion ID、证据类型、证据来源、冻结命令、退出码、摘要、细节
和 verification check IDs；整体 `VerificationResult` 使用 `VERIFIED /
PARTIALLY_VERIFIED / REGRESSED / UNVERIFIED`，同时保留 Target、Regression、Sanity、
baseline failures、current failures、new failures、Critical 分类和人工确认项。

命令按固定的 Cheap Evidence First 顺序运行：SANITY → TARGET → REGRESSION。Sanity
失败时停止后续昂贵 checks。Target 证明目标行为；Sanity 只证明基本工程完整性；
Regression 必须与同一 verification ID 的 baseline 比较。若测试输出能识别测试名，则比较
失败项集合；修改前已经失败且修改后保持相同的测试不会成为新 regression。无法提取名称
时，只有 baseline PASS 变成当前 FAIL 才明确算作新增 regression。

整体状态采用离散规则：

- `VERIFIED`：所有 Critical 都有环境证据且 PASS、必要 Sanity PASS、无新增 regression，
  也没有失败的自动 Non-critical criterion。
- `PARTIALLY_VERIFIED`：满足 VERIFIED 的核心条件，仅剩 Non-critical HUMAN 项。
- `REGRESSED`：发现 baseline 原本通过的项目失败，或逐项比较出现新增失败；优先级最高。
- `UNVERIFIED`：Critical 缺少独立证据、Critical/必要 Sanity 失败，或其他证据不足。

VERIFIED 和 PARTIALLY_VERIFIED 会更新 pending ChangeSet 并请求 stable checkpoint，最终
finish 才能进入 DONE。REGRESSED 会先尝试最近 ChangeSet Undo，无法安全撤销时才回退到
stable checkpoint，并进入 DEBUGGING。Target FAIL 但没有新增 regression 时保留修改进入
DEBUGGING，不自动回滚。Critical 缺少自动证据时保持非 DONE 并报告人工确认需求。

`failed_finish_attempts` 记录失败的结束申请。达到固定 guardrail 后，DEBUGGING context
重新包含完整冻结 criteria、失败 CriterionResult 和证据，重复 finish 不能改变验收规则。
只有 VerificationEngine 解释为 Criterion PASS 的环境结果才能写入 `confirmed_facts`。

离线测试与隔离 Git demo：

```powershell
python -m unittest -v test_verification_engine.py
python demo_evidence_gated_verification.py
```

## Failure-aware Recovery

失败现在通过 `FailureEvent` 单独记录，不再只保留一段普通错误文本。事件包含 ID、粗粒度
failure type、file/symbol/test location、环境 evidence、相关 ChangeSet 和 Criterion、当时的
hypothesis/attempt、diagnostic hints、确定性 fingerprint、repeat count、step、phase、恢复
结果以及失败编辑的 action signature。Failure history 与 `confirmed_facts` 完全分离。

`FailureClassifier` 只附加 `BUILD_FAILED`、`TEST_FAILED`、`TIMEOUT`、`STALE_EDIT` 等粗粒度
标签。它不会用分类结果替代错误内容：compiler/test/runtime 的 command、stdout、stderr、
exit code、timeout 和原 ToolResult 截断标记都保存在 `evidence` 中。为了控制上下文，stdout
与 stderr 分别使用固定字符上限和首尾保留策略，并分别标记是否发生二次截断；因此某个流
过长不会导致另一个流从 evidence 中消失。Diagnostic hints 只是排查建议，不是事实或根因
断言。

`FailureMemory` 使用结构化稳定字段构造 fingerprint：测试失败使用 test/file/error category，
构建失败使用 file/error category，超时使用规范化 command/current step，编辑失败使用
file/symbol/criterion。临时路径、时间戳、随机 ID、完整行号和完整 stderr 不作为主要字段。
相同 fingerprint 再次出现时追加新的历史事件并递增 `repeat_count`。

普通失败首先进入 DEBUGGING。达到可配置的 `MAX_REPEAT_FAILURES` 后，如果存在多个 pending
ChangeSets 且现有 CheckpointManager 明确允许安全 rollback，可以请求回到最近稳定点；
随后进入已有 PLANNING phase。Acceptance Criteria、Verification Contract、baseline 和用户
任务保持冻结，不会重新生成。

重新规划分两次结构化提交：先提交 Failure Analysis，包含 previous hypothesis、observed
evidence、previous attempts、之前尝试为何不充分、remaining possibilities、revised
hypothesis 和 revised plan；下一次 PLANNING context 明确包含该分析后，才开放 Revised
Execution Plan。系统不判断新旧计划在语义上是否相似，也没有 LLM judge、embedding 或
fuzzy score。

Duplicate Failed Action Guard 只比较完全相同的 file、symbol/anchor、operation、intent 和
new block hash。命中时返回 `DUPLICATE_FAILED_ACTION`、原 failure ID 和原始失败 evidence；
思想相似但具体修改不同的尝试不会被阻止。

REGRESSION 的 Undo/Stable Checkpoint rollback 仍只由 VerificationEngine 执行，Failure
Recovery 仅记录其现有 recovery result。只有缺少证据、没有真实 Critical FAIL 的
UNVERIFIED 不创建 FailureEvent，继续走人工确认流程。

离线验证：

```powershell
python -m unittest -v test_failure_recovery.py
python demo_failure_aware_recovery.py
```
