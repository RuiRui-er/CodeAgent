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
当前 step、phase 和 `APPLIED` 状态。ChangeSet 只描述发生的修改，不代表 VERIFIED，
也不会触发测试、编译、Git commit、snapshot 或 rollback。

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
```

离线观察四个 phase 的上下文组成（不调用模型 API，也不修改 demo 文件）：

```powershell
python demo_context.py
python demo_tool_safety.py
python demo_structured_edit.py
```
