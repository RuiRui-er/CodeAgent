# CodeAgent 演示视频面试问答

> 使用场景：老师先观看 CSV 正常闭环与 Regression Recovery 两段演示，再围绕视频追问设计思路。
>
> 回答目标：证明自己理解 Agent 为什么这样运转、每个机制解决什么风险，以及为什么没有使用 Agent 框架；不需要背具体代码行。

## 1. 先准备好的一分钟总述

这个项目没有使用 Agent 框架。它的核心是一个由状态、事件和确定性组件组成的循环：模型负责理解任务和提出工具动作，但模型不直接决定任务是否成功。任务开始时先确定验收标准和验证方法，并在修改前运行 baseline；执行阶段的文件修改会形成 ChangeSet，此时只代表 `APPLIED`，仍然是 `UNVERIFIED`；模型调用 `finish` 也只是请求进入验证。最终由 VerificationEngine 执行事先冻结的 TARGET、REGRESSION 和 SANITY 检查，通过 Evidence Gate 后才能创建 Stable Checkpoint 并进入 `DONE`。如果新功能通过但破坏了原有行为，系统会判定为 `REGRESSED`，优先撤销相关 ChangeSet，然后携带真实失败证据进入 `DEBUGGING`。

这套设计的核心原则是：**模型负责提出行动，确定性系统负责约束行动和裁决结果。**

---

## 2. 老师看完第一个实验最可能问什么

### Q1：第一个实验到底展示了什么？

它展示的是一个正常的端到端闭环：用户提交 CSV 工具任务，系统展示 Acceptance Criteria 和冻结的 Verification Contract，在修改前运行 baseline；模型通过 Structured Editing 修改 `csv_tool.py`，生成 ChangeSet；修改后进入 VERIFYING，依次运行 SANITY、TARGET 和 REGRESSION；所有关键证据通过后创建 Stable Checkpoint，最后进入 `DONE`。独立 hidden evaluator 再从系统外检查结果，用来观察是否发生 False Success。

需要强调：终端中的状态不是 renderer 编出来的。Rich 层只读取真实状态、事件、工具结果和验证结果，不参与状态迁移和成功判定。

### Q2：为什么要先定义 Acceptance Criteria？

因为自然语言任务通常只描述“想要什么”，没有精确说明“怎样算完成”。Acceptance Criteria 把任务拆成可观察条件，例如：缺失 age 不应使整个批处理崩溃、非法 age 不影响合法记录、`--min-age` 正确过滤、不带参数时保持原行为。

如果没有这一步，模型可能只实现最显眼的新功能，然后凭主观判断宣布完成。验收标准相当于提前固定成功定义，减少执行过程中“做完以后再降低标准”的空间。

### Q3：Acceptance Criteria 和 Verification Contract 有什么区别？

- Acceptance Criteria 描述“必须成立的行为”。
- Verification Contract 描述“用什么确定性输入、命令和断言证明该行为成立”。

例如，“非法 age 不影响其他合法记录”是验收标准；构造包含合法行和 `abc` age 的 CSV、运行 CLI、断言退出码为 0 且合法行仍输出，是对应的 Verification Contract。

前者是目标，后者是证据获取方式。将两者分开，既方便人理解任务，又使机器可以执行验证。

### Q4：项目没有现成测试，Agent 为什么还能验证？

没有测试框架不等于无法验证。这个 CSV 工具本身有稳定的 CLI 输入输出，因此系统可以在修改前冻结最小 CSV 数据、运行命令和预期输出。验证命令临时创建输入文件、调用真实程序、检查退出码和输出，然后清理文件。

这比为了演示临时引入 pytest 更贴近产品行为，也避免把“找不到测试”错误地解释成 Regression 不适用。

### Q5：为什么验证输入必须在修改前确定？

如果修改完成后才让模型决定测什么，模型可能有意或无意地选择更容易通过的用例，形成自证偏差。修改前冻结输入、命令和断言，使实现者不能在看到实现结果后改变评分规则。

它不保证测试绝对完备，但保证同一次运行中的成功标准不会漂移。

### Q6：Baseline 有什么作用？为什么不只看修改后的 PASS？

Baseline 记录修改前每个冻结检查的行为。它有两个作用：

1. 对 TARGET，证明问题或缺失功能在修改前确实存在，例如 `--min-age` 修改前失败、修改后通过。
2. 对 REGRESSION，记录原本正常的行为，例如不带参数时正常 CSV 的输出，修改后必须继续保持。

仅看修改后的 PASS 无法区分“新修好的行为”和“本来就正常的行为”，也无法判断某个失败是不是本次修改新引入的。

### Q7：视频里为什么有些 Baseline 是 FAIL？Baseline FAIL 不是坏事吗？

要区分证据类型：TARGET 的 baseline 失败通常是预期的，它证明目标问题真实存在；REGRESSION 和 SANITY 的 baseline 应当通过，因为它们代表修改前必须保留的能力和可运行环境。

所以不能把 baseline 简化成“必须全部 PASS”。关键是按证据类型解释其含义。

### Q8：为什么 APPLIED 后仍显示 UNVERIFIED？

`APPLIED` 只说明编辑已经成功落到文件中，Resolver 找到了目标位置并生成了 ChangeSet。它不说明语法正确、功能正确，也不说明没有破坏旧行为。

如果把文件写成功等同于任务成功，Agent 很容易产生 False Success。因此 APPLIED 和 VERIFIED 必须分离：前者是工具层事实，后者是证据层结论。

### Q9：为什么要 Structured Editing，不让模型随意重写文件？

Structured Editing 要求模型说明文件、symbol、操作类型、意图和替换内容，再由 Resolver 在当前文件中定位。这带来三个收益：

- 限制修改范围，降低整文件误写风险；
- 可以形成结构化 ChangeSet，为证据关联和 Undo 提供基础；
- stale 或 ambiguous 时明确拒绝，而不是猜一个位置继续改。

它不是为了追求复杂，而是为了让修改可解释、可追踪、可恢复。

### Q10：为什么 `finish` 不能直接进入 DONE？

因为 `finish` 是模型对自己工作的主观声明。如果它能直接完成任务，就相当于让实施者同时担任裁判。

当前设计中，`finish` 只产生“请求最终验证”的事件，系统随后进入 VERIFYING 并执行冻结的 Verification Contract。只有 Evidence Gate 判定所有 CRITICAL 条件有合法证据，才能进入 DONE。因此 `finish` 是控制信号，不是成功证据。

### Q11：Evidence Gate 具体防止什么？

它防止以下情况被误报为成功：

- 代码成功写入，但没有运行验证；
- 新功能通过，但原有行为回归；
- 只运行了 SANITY，没有运行 TARGET；
- 命令无法执行，模型却口头声称完成；
- 仍有 CRITICAL 条件失败或缺少证据。

Evidence Gate 的价值不在于让模型“更聪明”，而在于把最终完成权从概率模型移到确定性规则。

### Q12：为什么还要 hidden evaluator？它和 VerificationEngine 重复吗？

不重复。VerificationEngine 是 Agent 正常工作流的一部分，检查的是修改前冻结且 Agent 可见的合同。Hidden evaluator 位于 Agent workspace 之外，Agent不能读取，用于实验评价，检查额外边界案例并判断是否 False Success。

它们分别回答：

- VerificationEngine：Agent 是否依据自己的正式证据合同完成任务？
- Hidden evaluator：这个成功结论在独立评测下是否仍成立？

hidden evaluator 不驱动 Agent 的 phase，也不会偷偷帮助 Agent 修改代码。

### Q13：为什么成功后才创建 Stable Checkpoint？

Checkpoint 表示一个经过验证、可以信任的恢复基点。如果每次编辑后都创建“稳定”checkpoint，错误实现也会被标记为稳定，回滚语义就失去了可信度。

因此 ChangeSet 可以在 APPLIED 后存在，但 Stable Checkpoint 只能在合法最终验证状态后创建。

---

## 3. 老师看完第二个实验最可能问什么

### Q14：第二个实验为什么第一次修改会故意产生回归？这算伪造吗？

第二个实验明确标记为 deterministic recovery fixture。它固定的是一个合理但有缺陷的候选编辑：`--prefix VIP` 能输出 `VIP:alice`，但未传 prefix 时错误输出 `:alice`。之所以固定候选编辑，是因为无法保证任意大模型在两分钟录屏中自然且稳定地产生同一种回归。

没有固定或伪造的是：工具是否真正应用修改、TARGET 是否通过、REGRESSION 是否失败、ChangeSet 是否能安全 Undo、phase 是否进入 DEBUGGING、第二次验证是否通过、checkpoint 是否创建，以及 hidden evaluator 的结果。这些都由生产组件真实执行。

因此它是用于复现 recovery pipeline 的故障注入，不是假日志，也不声称第一次错误是模型自然生成的。

### Q15：既然是固定 fixture，它还能证明 Agent 能力吗？

它不能单独证明模型在任意任务上都有高修复成功率；它证明的是系统机制在已知回归输入下能否真实闭环：检测、归因、恢复、携带证据进入 DEBUGGING、再次修改和重新验证。

实验一用于展示真实模型执行正常任务；实验二用于确定性测试异常控制流。两者分别验证“行动能力”和“系统机制”，不能混为一谈。

### Q16：为什么新功能已经 PASS，整体仍然是 REGRESSED？

因为任务不仅包含新功能，也包含兼容性要求。TARGET PASS 只能证明 `--prefix` 已实现；REGRESSION FAIL 证明修改破坏了修改前正常的默认输出。整体成功必须同时满足新增行为和保留行为，所以系统判定 `REGRESSED`，而不是 `VERIFIED`。

这体现了 Agent 不能只追求局部目标，还要对修改造成的系统性影响负责。

### Q17：系统怎样知道这是“新回归”，而不是原来就失败？

它将当前 REGRESSION 结果与同一个冻结检查的 baseline 比较：baseline 中 `V_DEFAULT` 是 PASS，修改后变成 FAIL，因此这是本次修改新引入的失败。视频中的 `PASS → FAIL` 就是这个判断的可视化。

如果 baseline 本来就是 FAIL，就不能简单称为“新回归”，需要按原始失败处理或承认基线无法证明该兼容性。

### Q18：为什么恢复时优先 ChangeSet Undo？

因为这次回归与最近一个局部 ChangeSet 有明确关联，而且文件没有出现无法安全反向应用的后续冲突。Undo 只撤销相关编辑，影响范围最小，也保留其他可能已经验证的工作。

如果局部 Undo 不安全，例如同一文件已有后续依赖修改、内容已经漂移或无法唯一反向应用，才退回 Stable Checkpoint Rollback。恢复策略遵循“能局部恢复就不做全局恢复”。

### Q19：ChangeSet Undo 和 Stable Checkpoint Rollback 有什么区别？

- ChangeSet Undo 针对一个具体编辑，粒度小，适合最近且可安全反向应用的失败修改。
- Stable Checkpoint Rollback 恢复到最近一个已验证的整体状态，粒度大，适合局部历史已经混乱或安全 Undo 条件不成立的情况。

ChangeSet 是“编辑级撤销”，Checkpoint 是“状态级恢复”。两者职责不同，不应混成同一种回滚。

### Q20：为什么 Undo 之后还要进入 DEBUGGING？代码不是已经恢复了吗？

Undo 只消除了回归，使工作区回到修改前或较安全的状态，但用户的新需求仍然没有完成。因此不能回到 DONE，也不能把恢复成功当作任务成功。

DEBUGGING 的任务是利用失败证据调整实现策略：保留 `--prefix` 新功能，同时修复默认行为。恢复解决“先止损”，DEBUGGING 解决“怎样重新完成任务”。

### Q21：DEBUGGING 和 EXECUTING 的本质区别是什么？

EXECUTING 主要依据原始计划推进实现；DEBUGGING 依据一个已经发生的具体失败推进，输入中增加了 FailureEvent、失败检查、baseline/current 差异、相关 ChangeSet、恢复结果和历史尝试。

也就是说，DEBUGGING 不是简单换一个 phase 名称，而是让下一轮决策以失败证据为中心，避免模型忽略刚才发生的错误。

### Q22：视频里的 FailureEvent 和 fingerprint 有什么意义？

FailureEvent 把原始工具或验证失败规范化，例如这次是 `REGRESSION_DETECTED`，关联 `V_DEFAULT`、`AC_DEFAULT` 和 `change_0001`。fingerprint 将失败类型和关键位置归一化成稳定标识，例如 `regression_detected:v_default:ac_default`。

它们的作用是：

- 为 DEBUGGING 提供结构化证据，而不是只给一段长日志；
- 判断连续失败是否实际上是同一个问题；
- 为 repeated failure 和是否需要 REPLAN 提供确定性依据；
- 避免完全相同的失败动作被无限重复。

### Q23：为什么第二个实验没有继续演示 repeated failure 和 Replanning？

因为两分钟演示的重点是把一条异常闭环讲清楚。Regression、Undo、DEBUGGING 和重新验证已经覆盖最关键的恢复链路。为了强行展示 Replanning 而连续制造相同错误，会降低真实性并让主线变复杂。

Replanning 机制保留在系统中，但不代表每次 DEBUGGING 都应该触发；只有重复相同 fingerprint、缺少进展时才值得重新规划。

### Q24：DEBUGGING 会不会陷入“读文件—失败—再读文件”的循环？

这是实际调试中出现过的风险。系统有主循环 `max_steps` 作为最终熔断；同时对重复检查无进展和 `STALE_EDIT` 增加收敛约束，要求停止重复读取，并通过 `finish` 让冻结的验证合同判断当前 ChangeSet 是否已经足够。

这里的设计原则是先用低成本的确定性收敛规则解决明确循环，不急于引入复杂的反思模型或多 Agent 协商。

### Q25：第二次修复后为什么必须完整再跑一遍验证？

因为第二次 ChangeSet 也是新的未验证修改。即使它针对刚才的 REGRESSION，也可能再次破坏 TARGET 或引入语法问题。因此不能只重跑失败的 `V_DEFAULT` 就直接完成；视频中重新展示 SANITY、TARGET 和 REGRESSION，最终三类证据同时通过才进入 DONE。

---

## 4. 老师最可能追问的总体设计决策

### Q26：为什么不用 Agent 框架？你自己实现了哪些核心能力？

任务明确要求不能使用 Agent 框架，因此项目只使用普通的模型 API 和 Python 模块。自己实现的核心包括：

- 主 Agent Loop 和 `max_steps` 熔断；
- phase、Event 和 Orchestrator 驱动的状态机；
- phase-aware context 组装；
- 工具 schema、工具执行和 workspace 边界保护；
- Structured Editing、Resolver、ChangeSet；
- Acceptance Criteria、Verification Contract 和 baseline；
- VerificationEngine、Evidence Gate 和 checkpoint；
- FailureEvent、fingerprint、Undo、Rollback、DEBUGGING 和 Replanning。

重点不是“重新发明一个框架”，而是把本任务需要的最小控制流显式实现，使每个状态和决策都容易解释。

### Q27：LLM 在系统中到底负责什么，不负责什么？

LLM 负责：理解任务、阅读上下文、提出计划和工具调用、根据失败证据选择下一步修改。

LLM 不负责：直接写 workspace、绕过工具权限、直接修改 phase、直接宣布 VERIFIED、创建 Stable Checkpoint、决定回滚是否真实成功。

这些边界把创造性和确定性分开：模型适合提出候选方案，程序适合执行安全规则和验证证据。

### Q28：为什么使用状态机，而不是在 prompt 里告诉模型当前该做什么？

只靠 prompt 是软约束，模型可能跳过验证、重复编辑或口头宣布完成。状态机使允许的转移成为程序规则，例如 PLANNING 不能直接 DONE、EXECUTING 的 finish 只能进入 VERIFYING、REGRESSED 必须进入 DEBUGGING。

状态机也提高可观察性：视频里的每次状态迁移都有事件原因，出问题时可以判断是模型决策、工具执行还是生命周期控制的问题。

### Q29：为什么必须由 Orchestrator 统一修改 phase？

如果 ToolExecutor、VerificationEngine 和主循环都能直接写 phase，同一个结果可能触发冲突迁移，测试也难以判断是谁改变了状态。当前设计让模块只返回事实或结果，主循环将其翻译为 Event，再由 Orchestrator 根据转移表唯一更新 phase。

这样 Tool 层回答“发生了什么”，Verification 层回答“证据是什么”，Orchestrator 回答“生命周期下一步是什么”。

### Q30：Event 的价值是什么？直接调用下一个函数不行吗？

小型脚本可以直接调用，但随着验证失败、回归、Undo、人工暂停、重复失败和 Replan 增多，直接调用会把“发生的事实”和“控制流响应”耦合在一起。

Event 提供稳定语义，例如 `EDIT_APPLIED`、`VERIFICATION_REGRESSED`、`FINISH_REQUESTED`。它让同一种事实由统一规则处理，也让日志能解释“为什么从 A 进入 B”。

### Q31：Phase-aware Context Management 解决什么问题？

模型上下文有限，而且不同阶段需要的信息不同：

- PLANNING 需要任务、仓库证据和成功标准；
- EXECUTING 需要当前步骤、相关代码和待验证项；
- DEBUGGING 需要失败证据、相关 ChangeSet、baseline/current 差异和恢复结果；
- REPLANNING 需要重复失败历史和已尝试策略。

如果每轮都塞入完整历史，噪声和成本会不断增加；如果只给最后一条错误，模型又缺少因果信息。Phase-aware context 是在“足够的信息”和“可控的上下文”之间取平衡。

### Q32：`max_steps` 能完全防止无限循环吗？

它能保证主 execution loop 最终停止，但不是所有模型请求的全局预算，因为 initial planning、repair 和 replanning 还有各自的局部调用。因此准确说法是：`max_steps` 是主循环熔断器，而不是整个程序绝对的 token 或请求上限。

当前这样实现简单且有效；更完整的方案是未来增加统一 Budget，同时统计模型请求、工具调用和验证耗时，但对于当前项目不是 P0。

### Q33：为什么不做多个 Agent，让 Planner、Coder、Reviewer 相互讨论？

当前任务规模小，核心风险是状态和证据是否闭环，而不是角色数量不足。多 Agent 会增加上下文传递、冲突决策、成本和难以解释的协作协议，却不能自动解决“谁有最终裁决权”。

所以当前选择一个 LLM 决策者，加确定性的工具、验证和状态控制。只有未来任务能明确证明并行探索或专业分工带来收益时，才值得引入多 Agent。

### Q34：为什么不用向量数据库或 RAG？

当前 workspace 小，Agent 可以通过目录、搜索和按需读文件定位信息。引入 RAG 会增加索引更新、切块、召回错误和实现复杂度，且代码修改后索引一致性也需要处理。

对于当前任务，phase-aware 的精确文件和 symbol 上下文更直接。RAG 只有在仓库规模大到搜索和上下文截取成为实际瓶颈时才有必要。

### Q35：为什么不让另一个 LLM 当 Judge？

LLM Judge 对开放性质量评价可能有用，但这里的大部分标准是可执行的 CLI 行为、退出码和输出，可以用确定性命令验证。再引入一个概率模型会增加成本和不稳定性，也使最终裁决难以解释。

因此当前优先使用可执行证据；只有无法自动化的主观质量标准，才考虑 HUMAN 或 LLM Judge，并且要明确其证据等级。

---

## 5. 人工确认相关问题

### Q36：视频里为什么没有把 HUMAN verification 放进最终闭环？

当前 CLI 没有实现同一 AgentState 的持久化、暂停和恢复。如果允许 HUMAN Acceptance Criterion，程序暂停后无法可靠地从原状态继续，流程会出现半闭环。因此当前 planning 明确禁用 HUMAN criterion，而不是假装支持。

这是有意的范围控制：没有闭环的功能宁可明确禁用，也不在视频中伪造。

### Q37：工具人工确认和 HUMAN verification 是一回事吗？

不是。

- 工具人工确认回答“是否允许执行这个有副作用或高风险的操作”。
- HUMAN verification 回答“某条验收标准是否已经被人工证明通过”。

前者属于安全授权，不应被当作功能正确性的证据；后者属于 Evidence Gate，需要可恢复的人工证据流程。两者必须分开，否则用户点一次“允许执行”就可能被错误解释为“任务验证通过”。

### Q38：如果视频要加入人工确认，放在哪里最合理？

最合理的是确认一个有副作用但不决定正确性的工具操作，例如任务完成后选择是否导出 DEBUGGING 报告。用户拒绝导出不应改变代码的 VERIFIED 状态。

不建议让用户手动确认“代码正确”，因为当前 CSV 和 prefix 行为都可以自动验证；强行加入人工判定反而削弱 Evidence Gate 的可信度。

---

## 6. 老师可能提出的尖锐质疑

### Q39：你的 Verification Contract 也是模型生成的，模型写错测试怎么办？

这是当前系统的重要边界。通用模式下，模型提出合同，本地 schema 和语义规则检查结构、证据类型覆盖、命令可执行性等，但不能证明测试逻辑绝对正确。因此还需要 baseline 运行、hidden evaluator 和人工审查来降低风险。

视频 benchmark 为了可复现，使用经过审查的冻结合同，真实模型负责实施。这不是掩盖问题，而是明确区分“演示核心闭环”和“开放任务中自动生成测试的可靠性研究”。

未来值得做的是增加合同质量检查和任务特定 invariant，而不是用另一个模型随意投票。

### Q40：固定 Verification Contract 是否意味着这不是真 Agent？

不是。Agent 的核心不只在于生成计划，还在于根据当前 workspace 选择并执行动作、处理工具结果、推进状态、面对验证失败进行恢复。固定 benchmark 合同类似软件工程中的测试规范：它约束目标，但不预先给出实现。

同时应诚实说明，视频 live 模式展示的是“冻结演示计划 + 真实模型执行”，不是完全开放式自主规划。开放式 planning 能力存在于系统中，但不拿随机性较高的输出冒充稳定录屏结果。

### Q41：Rich 界面会不会伪造状态？

RichConsoleRenderer 是 presentation layer，只接收 AgentState、TransitionResult、ToolResult 和 VerificationResult 并格式化。它不写 phase、不改变 verification status、不执行 Undo，也不创建 checkpoint。

此外每次运行都保存核心原始日志和结果文件，可对照验证终端摘要是否来自真实内部对象。

### Q42：Hidden evaluator PASS 是否能证明系统绝对正确？

不能。它只说明实现通过了这组独立隐藏用例，不能证明所有输入和所有环境下都正确。正确表达应是“在公开合同和当前隐藏测试覆盖范围内没有发现 False Success”。

工程验证提供的是有边界的证据，而不是数学意义上的全称证明。

### Q43：你的系统是不是设计过度了？一个 CSV 工具需要这么多模块吗？

如果目标只是一次性修 CSV，确实不需要完整 Agent。但项目研究的是可解释、可恢复的编码 Agent 控制流，CSV 只是最小可观察实验载体。

同时设计保持了克制：没有引入多 Agent、RAG、LLM Judge、复杂任务图或分布式执行。保留的模块都对应视频中可见的问题：状态跳转、修改可追踪、回归检测、证据裁决和失败恢复。

### Q44：你认为当前最需要改进的地方是什么？

优先改进项是：

1. 通用 planning 下 Verification Contract 的语义质量仍依赖模型，需要更强的本地一致性检查。
2. `max_steps` 不是统一的全局请求预算，可以增加跨 planning/execution/replanning 的 Budget。
3. HUMAN verification 当前被明确禁用；如果未来确实需要，应先实现 AgentState 持久化和 resume。
4. Baseline-relative regression 对失败信息归一化仍可增强，避免不同格式的同一错误被误判。

不建议当前就加入多 Agent、向量数据库或复杂反思树，因为它们不会优先解决流程正确性问题。

### Q45：这个项目最重要的设计取舍是什么？

最重要的取舍是没有追求“模型完全自主”，而是追求“自主行动与确定性治理的平衡”。模型越自由，越可能产生有创造力的方案，也越可能跳步、误判或重复；规则越多，系统越稳定，但也可能僵化。

当前做法是把计划和修改选择留给模型，把 workspace 边界、phase 转移、证据要求、Undo 和最终完成权交给程序。这个边界既能展示 Agent 能力，也能解释为什么结果值得信任。

---

## 7. 反问式追问：老师可能继续问“为什么不……”

### 为什么不在每次修改后立刻跑全部测试？

小项目可以，但大项目成本高。当前最终验证一定运行完整冻结合同；增量验证可按当前步骤运行相关检查。原则是风险越高、修改越接近完成，验证范围越完整。

### 为什么不在 Regression 后直接让模型继续改，而要先 Undo？

如果保留已知回归状态继续叠加修改，后续失败归因和局部恢复会更困难。能安全 Undo 时先回到较干净状态，再根据证据修复，因果关系更清楚。

### 为什么不把 hidden evaluator 结果反馈给 Agent？

因为 hidden evaluator 是实验测量工具。如果反馈给 Agent，它就变成公开验证合同的一部分，失去独立评估 False Success 的意义。

### 为什么不记录完整 chain-of-thought？

系统只展示结构化 Decision Summary、工具调用和证据。完整内部推理既不稳定，也不是验证正确性的可靠依据。面试需要解释的是可观察决策和系统约束，而不是依赖不可审计的长篇思维文本。

### 为什么不自动提交或推送代码？

提交和推送是外部持久化操作，影响用户仓库历史，应该由明确任务授权驱动。Agent 完成代码验证不等于获得修改远程仓库的权限。

---

## 8. 视频画面与回答要点速查

| 视频画面 | 老师可能问 | 一句话核心回答 |
|---|---|---|
| Acceptance Criteria | 为什么先列标准？ | 修改前固定成功定义，防止做完后降低标准。 |
| Verification Contract 已冻结 | 为什么冻结？ | 避免实现者根据结果临时换测试，自证成功。 |
| Baseline TARGET FAIL | 为什么失败也保留？ | TARGET FAIL 证明问题存在；REGRESSION 应保留 PASS。 |
| ChangeSet APPLIED / UNVERIFIED | 为什么还没成功？ | 写入成功是工具事实，不是功能证据。 |
| finish 后进入 VERIFYING | 为什么不 DONE？ | 模型不能既实施又裁决，finish 只是验证请求。 |
| FAIL → PASS | 说明什么？ | 同一冻结用例证明目标行为确实发生改变。 |
| PASS → PASS | 说明什么？ | 修改前正常行为在修改后仍保持。 |
| TARGET PASS / REGRESSION FAIL | 为什么整体失败？ | 局部新功能不能抵消兼容性破坏。 |
| ChangeSet UNDONE | 为什么局部撤销？ | 与失败关联清楚时采用最小影响恢复。 |
| VERIFYING → DEBUGGING | DEBUGGING 多了什么？ | 增加 FailureEvent、差异、ChangeSet 和恢复证据。 |
| fingerprint | 有什么用？ | 识别同一失败，抑制重复动作并触发 Replan。 |
| Stable checkpoint | 为什么最后才创建？ | 只有证据门通过的状态才配称为稳定。 |
| Hidden Evaluation PASS | 是否绝对正确？ | 只证明当前独立隐藏用例范围内未发现 False Success。 |

---

## 9. 面试时不要说错的几句话

不要说：

- “Baseline 必须全部 PASS。”
- “ChangeSet APPLIED 就表示修改正确。”
- “finish 会让 Agent 完成任务。”
- “第二个实验是模型自然写错后自己修好。”
- “hidden evaluator 保证代码绝对正确。”
- “max_steps 限制了整个程序所有 LLM 调用。”
- “工具人工确认就是 HUMAN verification。”
- “用了状态机就不会出现任何循环。”

建议改成：

- “TARGET baseline 可以预期失败；REGRESSION baseline 用于证明原行为。”
- “APPLIED 与 VERIFIED 有意分离。”
- “finish 只触发最终验证。”
- “第二个实验固定候选错误，但 recovery pipeline 和所有证据都是真实执行的。”
- “hidden evaluator 在当前覆盖范围内检查 False Success。”
- “max_steps 是主 execution loop 的熔断器。”
- “工具确认管理权限，HUMAN verification 管理验收证据。”

---

## 10. 最后 30 秒总结模板

我这个项目最想证明的不是模型能生成多少代码，而是一个不依赖 Agent 框架的编码 Agent 怎样形成可信闭环。模型可以提出计划和编辑，但每次文件修改都只是未验证的 ChangeSet；成功定义和验证方法在修改前冻结；finish 不能绕过 Evidence Gate；回归通过 baseline/current 对比识别，并优先局部 Undo；DEBUGGING 得到的是结构化失败证据，而不是一句“再试一次”。我选择这些机制，是为了让系统简单、可解释、可恢复，同时明确承认开放式验证生成、全局预算和 HUMAN resume 仍是后续边界。
