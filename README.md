# Minimal Coding Agent Baseline

这是一个刻意保持简单的 Coding Agent。它不使用 LangChain、OpenAI Agents SDK、
AutoGen 等 Agent 框架或 SDK；Agent 循环、消息历史、工具定义与执行、结果回传、
终止条件和错误处理均自行实现。状态数据与上下文选择分别拆分在
`agent_state.py` 和 `context_manager.py`，入口循环仍位于 `coding_agent.py`。

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
5. 模型使用全部工具实施计划，直到给出结论或达到最大步数。

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

## 本地工具

- `list_files`：列出 workspace 中的文件和目录；
- `read_file`：读取 UTF-8 文本文件；
- `write_file`：创建或完整覆写 UTF-8 文本文件，也用于修改；
- `search_text`：递归搜索文本并返回文件、行号和内容；
- `run_command`：通过 `subprocess.run` 在 workspace 中执行参数数组，采用
  `shell=False`，返回 stdout、stderr、exit code，并限制超时时间为 1–120 秒。

所有直接文件工具都会解析规范路径并检查其仍位于 workspace 内。命令固定以
workspace 为工作目录，且不经过 shell。需要注意：普通操作系统进程本身并不是强安全
沙箱；若要对恶意命令提供不可绕过的隔离，应在容器或受限系统账户中运行本程序。

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
```

离线观察四个 phase 的上下文组成（不调用模型 API，也不修改 demo 文件）：

```powershell
python demo_context.py
```
