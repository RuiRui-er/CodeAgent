# CodeAgent：证据驱动的自主代码智能体

CodeAgent 是一个面向 Python 项目的自主代码修改系统。它能够理解用户任务、制定执行计划、调用工具修改代码，并根据真实运行结果判断任务是否完成。

项目的重点是让模型具备自主决策能力，同时受到程序边界和环境证据的约束。系统通过阶段感知的上下文管理、可追踪的结构化代码编辑，以及证据驱动的验证与恢复，降低错误修改、虚假成功和重复失败的风险。

## 项目特点

### 完整的 Agent 生命周期

| 阶段 | 作用 |
| --- | --- |
| PLANNING | 理解任务，生成验收标准、验证契约和执行计划 |
| EXECUTING | 按计划读取项目、调用工具并实施代码修改 |
| VERIFYING | 运行目标、回归和基础健全性检查 |
| DEBUGGING | 根据真实失败证据定位问题并尝试修复 |
| DONE | 所需证据全部满足，任务正常结束 |
| FAILED | 达到步骤上限或遇到不可恢复问题 |

所有阶段迁移由 Orchestrator 统一处理。模型和工具不能直接修改当前阶段。

### 修改成功不等于任务成功

结构化代码编辑成功后，ChangeSet 的状态是 APPLIED 和 UNVERIFIED。这只表示修改已经写入文件，并不表示修改正确。

模型调用 finish 也不能直接进入 DONE。finish 只会申请最终验证，系统必须经过 VerificationEngine 和 Evidence Gate，得到合法验证结果后才能结束任务。

### 状态感知的上下文管理

系统不会把不断增长的完整历史原样发送给模型。ContextManager 会根据当前阶段选择信息：

- 规划阶段关注任务、项目发现和验收要求；
- 执行阶段关注当前步骤、剩余步骤、相关代码和 ChangeSet；
- 验证阶段关注验收标准、验证契约、Baseline 和当前结果；
- 调试阶段关注 FailureEvent、失败证据、相关修改和恢复结果。

任务内状态保存在 AgentState 中。最近工具动作保留五项；失败历史在本次任务中持续记录，重新规划时最多选取最近六条相关失败。单轮结构化上下文受约一万八千字符的总预算约束。

当前系统没有向量数据库或跨任务长期知识记忆。进程结束后，下一次任务不会自动继承上一次任务的经验。

### 结构化代码编辑与唯一定位

模型不会直接提交整文件内容，而是描述目标文件、符号、操作类型、定位锚点、修改意图和局部新旧内容。

EditResolver 依次使用 Python AST 符号范围、锚点附近局部内容和全文件唯一内容定位修改。只有目标能够唯一确定时才会写入文件；存在多个候选、内容过期或路径不安全时，系统会拒绝猜测式修改。

### 工具系统与工作区保护

| 工具 | 用途 |
| --- | --- |
| read_file | 读取工作区文件 |
| list_dir | 查看目录内容 |
| search_code | 搜索代码和符号 |
| apply_patch | 执行结构化局部修改 |
| run_command | 运行测试、编译和检查命令 |
| finish | 申请进入最终验证 |

WorkspaceGuard 会检查文件路径，禁止文件操作逃出指定工作区。CommandPolicy 将命令分为安全、需要人工确认和禁止三类。

该保护属于工作区级安全策略，不是操作系统级沙箱。处理不可信项目时仍建议使用容器或受限账户。

### 证据驱动的验证

规划阶段会在修改前冻结验收标准和验证契约，并用同一组检查采集 Baseline。最终验证依次运行：

1. SANITY：编译、导入等基础健全性检查；
2. TARGET：验证任务要求的新行为；
3. REGRESSION：比较修改前后的原有正常行为。

| 结果 | 含义 |
| --- | --- |
| VERIFIED | 关键目标、必要检查和回归要求全部满足 |
| PARTIALLY_VERIFIED | 核心自动验证通过，仅剩非关键人工项 |
| REGRESSED | 新修改破坏了 Baseline 中原本正常的行为 |
| UNVERIFIED | 关键证据不足或必要检查未通过 |

### ChangeSet、检查点与恢复

ChangeSet 记录 Agent 做过的局部修改，Stable Checkpoint 记录已经通过合法验证、可以安全保留的 Git 状态。

- ChangeSet Undo 用于撤销最近的安全局部修改；
- Stable Checkpoint Rollback 用于在局部撤销不安全时回到最近稳定点；
- 未验证的修改不能创建 Stable Checkpoint。

发现 REGRESSION 后，系统优先尝试 ChangeSet Undo；无法安全撤销时才考虑 Stable Checkpoint Rollback。恢复之后进入 DEBUGGING，并重新经过验证，不能直接宣布成功。

### 失败记忆与重复失败控制

失败会记录为结构化 FailureEvent，其中包含失败类型、命令输出、相关验收项、相关 ChangeSet 和确定性 fingerprint。

FailureMemory 使用 fingerprint 识别同类失败，并阻止完全相同的失败修改被重复应用。连续出现同类失败时，系统可以触发 REPLAN_REQUIRED。max_steps 为主循环提供最终上限，防止无限运行。

## 环境要求

- Python 3.10 或更高版本；
- Git；
- 使用真实模型时，需要兼容 OpenAI Chat Completions 和原生工具调用的模型服务。

## 快速运行

以下命令均在项目根目录执行。Windows 用户可以把 python 替换为解释器完整路径，例如 D:\Programs\anaconda3\python.exe。

### 配置真实模型

复制配置模板：Copy-Item .env.example .env

打开 .env，填写以下内容：

| 配置项 | 说明 |
| --- | --- |
| OPENAI_API_KEY | 模型服务密钥 |
| OPENAI_BASE_URL | 兼容接口地址 |
| OPENAI_MODEL | 模型名称 |

程序会自动读取项目根目录中的 .env，不需要每次在 PowerShell 中重新设置环境变量。

### 运行主 Agent

命令：python coding_agent.py --workspace demo_project "阅读项目，定位并修复测试暴露的问题，然后运行测试验证"

省略任务文本时，程序会在终端询问任务。使用 --max-steps 可以调整模型执行轮数，使用 --max-planning-steps 可以单独限制规划轮数。

## 运行测试

核心项目测试命令：python -m pytest -q --ignore=benchmarks --ignore=demo_project

benchmarks/runs 可能包含多次运行生成的同名测试文件，直接扫描整个仓库可能发生测试模块重名。demo_project 也故意保留了错误初始实现，因此核心测试命令明确排除这两个目录。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| coding_agent.py | 规划循环、执行循环与模型接口 |
| agent_state.py | 任务内结构化状态 |
| agent_orchestrator.py | Event 驱动的唯一阶段迁移入口 |
| context_manager.py | 按阶段构造模型上下文 |
| tool_registry.py | 工具定义和阶段权限 |
| tool_executor.py | 工具执行和 ChangeSet 创建 |
| tool_safety.py | 命令策略与工作区保护 |
| edit_resolver.py | 结构化修改定位 |
| verification_engine.py | 证据执行、比较与聚合 |
| checkpoint_manager.py | Stable Checkpoint、Undo 和 Rollback |
| failure_recovery.py | 失败决策与恢复策略 |
| failure_memory.py | fingerprint 和重复失败记忆 |

## 设计原则

CodeAgent 的核心原则是：模型可以提出计划和动作，但程序负责约束边界，环境负责提供证据，Orchestrator 负责推进状态。

因此，写入成功不是验证成功，finish 不是 DONE，模型判断也不能代替真实测试结果。
