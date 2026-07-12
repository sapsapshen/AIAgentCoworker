# 创建时测试报告（2026-07-12）

## 环境探测

| 角色候选 | 探测结果 | 可执行性 |
| --- | --- | --- |
| Codex | `codex-cli 0.142.0` | 命令存在；可运行 `codex exec --help` |
| Reasonix | 未发现 `reasonix` | 不能运行真实 worker |

Windows 上 npm 优先暴露 `codex.CMD`，不能直接由 Python 的 `shell=False` 启动。Relay 已识别同目录的 `codex.ps1` 并以 `powershell -File` 调用，保持子进程参数数组而非拼接 shell 命令。

## 已通过

- `python -m compileall -q handoff_relay tests`
- `python -m unittest discover -s tests -v`：4/4 通过。
- mock 端到端运行：规划者、执行者均可由任意 profile 注入；生成 plan、result、events、manifest 和 evidence。
- 默认配置 dry-run：已渲染 Codex 与 Reasonix 命令，未调用模型。

## 真实 CLI smoke test

使用只读 Codex profile 与 mock worker 启动了真实指挥者。子进程已成功进入 Codex，但服务端返回 HTTP 400：当前账户选择的 `gpt-5.6-terra` 要求比本机 `codex-cli 0.142.0` 更新的 Codex 版本。因此该真实链路未完成，原因是本机 Codex 版本与所选模型不兼容，而不是 Relay 协议或 Windows shim。

Reasonix 未安装，故没有把它伪装为已通过。安装并登录兼容的 Reasonix CLI 后，只需按其实际参数调整 `reasonix-worker.argv`，然后运行：

```powershell
python -m handoff_relay doctor --config relay.example.json
python -m handoff_relay run --config relay.example.json --task-file task.md
```
