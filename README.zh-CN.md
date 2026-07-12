# Agent Relay

`Agent Relay` 是一个本地、可审计的「指挥 Agent → 执行 Agent」编排器。它吸收了 Handoff Lab 的规划/执行分层，但把 Codex、Reasonix（或任何命令行 Agent）从流程逻辑中解耦：每个角色都是一个可配置 profile，而不是写死的产品名。

## 为什么要改

原方案的方向是对的：让规划和实现隔离、保留精简证据、在 worker 多次失败时兜底。但默认将 Codex/Reasonix、模型配置和 Web 服务耦合在一起，带来四个实际问题：

- 无法替换指挥者或 worker，也无法为不同仓库选择不同组合。
- worker 不可用时切换到 Codex 直接修改代码，会模糊职责和审计边界。
- CLI 参数与 Agent 的最终输出没有稳定、可验证的协议。
- 连接测试只检查某几个产品，不能证明一条完整委托链能跑通。

Agent Relay 的改进是：角色配置、版本化 JSON 合同、每次运行的事件账本、显式重试策略，以及可在没有真实模型的机器上跑完的 mock 集成测试。它**不会**在 worker 失败时静默让指挥 Agent 接管；需要切换角色时必须选择另一个 profile 并重新运行。

## 本机检查结果

本项目创建时检测到：

- `codex-cli 0.142.0` 可用，且支持 `codex exec`、`--output-schema` 与 `--output-last-message`；真实 smoke test 发现它与当前账户的 `gpt-5.6-terra` 不兼容，详见 [TEST_REPORT.zh-CN.md](TEST_REPORT.zh-CN.md)。
- 未检测到 `reasonix` 命令。因此默认 Reasonix profile 只用于 `doctor`/dry-run；端到端测试使用仓库内 mock agents，不伪造 Reasonix 已安装或已登录。

## 快速开始

仅依赖 Python 3.11+，无第三方运行时依赖：

```powershell
cd codex-reasonix-handoff
python -m handoff_relay doctor --config relay.example.json
python -m unittest discover -s tests -v
python -m handoff_relay run --config tests/fixtures/mock-relay.json --task-file tests/fixtures/task.md
```

真实执行前先渲染命令，不会调用 Agent：

```powershell
python -m handoff_relay run --config relay.example.json --task-file task.md --dry-run
```

## 配置模型

`relay.example.json` 中 `roles` 的键是任意角色名。`planner_role` 与 `worker_role` 指定这次流水线的两个角色；你可以创建 `codex-planner`、`reasonix-worker`、`local-worker`、`reviewer` 等 profile，并在不同配置文件中自由组合。

每个 profile 的 `argv` 是参数数组，而不是 shell 字符串。支持下列占位符：

- `{workspace}`：目标项目目录
- `{prompt}` / `{prompt_file}`：给 Agent 的文本任务
- `{input_file}`：上游产生的 JSON 合同
- `{output_file}`：本角色必须写入的最终 JSON
- `{schema_file}`：本角色输出对应的 JSON Schema
- `{run_dir}`：本次审计目录

`input` 可为 `prompt` 或 `json-file`；`output` 当前为 `json-file`。调用子进程时不经过 shell，因此任务标题和路径不会被命令注入。

### Codex 作为指挥者

示例 profile 使用本机已验证的 `codex exec`：通过 `--output-schema` 强制最终输出符合 `plan-v1`，并通过 `--output-last-message` 把 JSON 写入编排器指定文件。可按需调整 `model`、`profile`、sandbox 和超时；这些是 role 配置，不是 Relay 的硬编码。

### Reasonix 或其他 worker

示例 Reasonix profile 假定一个常见的文件接口：`reasonix run --input <file> --output <file>`。安装版本若参数不同，只修改该 profile 的 `argv` 即可，不需改 Python 代码。任何可读 `input_file`、写出 `output_file` 的 CLI 都可充当 worker。

## 合同与安全边界

流程固定为 `created → planned → working → completed/failed`，但具体 Agent 可替换。

1. 指挥者必须输出 `plan-v1`：目标、非空的可执行步骤与验收标准。
2. worker 接收该 plan，必须输出 `result-v1`：`passed` 状态、摘要和可追溯的证据路径。
3. 每一步都留下 `events.jsonl`、输入、输出、schema、stderr 和 `manifest.json` 于 `.agent-relay/runs/<id>/`。
4. 运行仅接受配置中明确列出的可执行文件；`doctor` 会报告缺失命令。没有「连续失败后自动越权」逻辑。

不要把密钥放进配置文件或任务文件。Agent Relay 不启动 Web 服务、不保存浏览器授权状态，也不会默认给任何子进程 YOLO 权限；具体权限由各 Agent 自己的 profile/CLI 参数和工作目录权限决定。

## 验证

`tests` 覆盖配置解析、缺失可执行文件诊断、dry-run 不执行、mock 规划者到 mock worker 的完整委托、及不合格 worker 输出的失败记录。mock 端到端测试是当前机器可重复的功能验收；`doctor` 另外确认 Codex 可用、Reasonix 未安装。

如果本机 Codex 已登录，可用只读权限跑一次真实指挥者 smoke test（worker 仍是 mock，因此不会声称 Reasonix 已验通）：

```powershell
python -m handoff_relay run --config tests/fixtures/codex-smoke.json --task-file tests/fixtures/task.md
```
