# Minimal Coding Agent Baseline

这是一个刻意保持简单的 Coding Agent。它不使用 LangChain、OpenAI Agents SDK、
AutoGen 等 Agent 框架或 SDK；Agent 循环、消息历史、工具定义与执行、结果回传、
终止条件和错误处理均在 `coding_agent.py` 中自行实现。

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

1. 把用户任务、系统提示、工具定义发送给模型；
2. 模型自主选择工具及参数；
3. 本地执行工具，将真实结果作为 tool message 追加到对话；
4. 再次调用模型，直到模型不再调用工具并给出结论，或达到最大步数。

程序会逐步打印模型消息、工具名、参数、执行结果和终止原因。

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
