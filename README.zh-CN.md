# Agent Relay

`Agent Relay`（产品名 AI Agent Coworker）是一个本地、可审计的「指挥 Agent → 执行 Agent」编排器。规划和执行分成两个可替换的角色：每个角色都是配置文件里的一个 profile，而不是写死的产品名。

完整使用说明、Web 界面和推荐配置见 [README.md](README.md)。本文保留设计边界，并和当前代码对齐。

## 设计边界

- 可以替换指挥者或执行者，也可以为不同仓库准备不同的配置文件。
- 执行者失败时，**不会**静默改由指挥者直接改代码。需要换角色时，换一个 profile 再跑一次。
- 角色参数是数组，不是 shell 字符串。子进程不经过 shell，任务标题和路径不会被拼进 shell 命令。
- 每次运行留下 `plan-v1` / `result-v1` 合同和 `events.jsonl`。合同按 JSON Schema 校验：未知字段会被拒绝，证据列表不能为空。
- `doctor` 只报告配置里写明的可执行文件是否可用，不会把缺失的命令换成另一个。

## 快速开始

需要 Python 3.11+，没有第三方运行时依赖。PATH 里没有 `python` 时，配置中的 `python` / `python3` 会落到当前解释器；Codex、CodeWhale、Reasonix 不会被自动替换。

```bash
python3 -m handoff_relay doctor --config relay.example.json
python3 -m unittest discover -s tests -v
python3 -m handoff_relay run --config tests/fixtures/mock-relay.json --task-file tests/fixtures/task.md
python3 -m handoff_relay run --config relay.example.json --task-file task.md --dry-run
```

`doctor` 在任一角色不可用时返回退出码 2。`relay.example.json` 的 Reasonix 角色在未安装时会显示 `missing`，这是预期结果。

本地页面：

- Windows：双击 `start-agent-coworker.bat`
- macOS / Linux：`./start-web.sh`

服务只监听 `127.0.0.1`。设置 `AGENT_COWORKER_NO_BROWSER=1` 可以不打开浏览器。

## 配置

`roles` 的键是任意角色名。`planner_role` 和 `worker_role` 选出这一次的两个角色。`argv` 支持：

- `{workspace}`：目标项目目录
- `{prompt}` / `{prompt_file}`：给 Agent 的文本任务
- `{input_file}`：上游产生的 JSON 合同
- `{output_file}`：本角色必须写入的最终 JSON
- `{schema_file}`：本角色输出对应的 JSON Schema
- `{run_dir}`：本次审计目录
- `{outputs_dir}`：本次交付物目录（`outputs/<run-id>/`）

`input` 可以是 `prompt` 或 `json-file`；`output` 目前是 `json-file`。未知占位符会直接失败，不会执行半截命令。

示例里的 Codex profile 使用 `codex exec`，用 `--output-schema` 和 `--output-last-message` 把 `plan-v1` 写到编排器指定的文件。Reasonix profile 假定 `reasonix run --input <file> --output <file>`；实际参数不同时只改这个 profile 的 `argv`。

## 运行记录

事件顺序是 `created → invoking → planned → invoking → completed/failed`。

1. 指挥者输出 `plan-v1`：目标、非空步骤、验收标准。
2. 执行者读这份计划，输出 `result-v1`：状态、摘要、至少一条证据路径。
3. `.agent-relay/runs/<id>/` 里有事件账本、输入、输出、schema、日志和 `manifest.json`。规划者进程失败或 JSON 不合法时，也会留下失败事件和 manifest。
4. 交付文件在 `outputs/<run-id>/`。

不要把密钥放进配置或任务文件。Web API 只接受仓库内的 JSON 配置，并拒绝跨站 POST。权限由各 Agent 自己的 CLI 参数决定；Relay 不会给子进程追加 YOLO 开关。

## 验证

`tests` 覆盖配置解析、合同校验、缺失可执行文件、dry-run、mock 规划者到 mock 执行者、不合格输出，以及本地 Web API。这些测试可以在没有 Codex / Reasonix / CodeWhale 的环境里重复跑。GitHub Actions 在 Python 3.11–3.14 上执行它们。

创建项目时的一次本机 Codex smoke test 记录在 [TEST_REPORT.zh-CN.md](TEST_REPORT.zh-CN.md)，那是历史快照，不是当前 CI 结果。
