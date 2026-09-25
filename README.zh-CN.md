# Codex 弹性调节器

[English](README.md)

让 Codex 不再对所有任务使用同一套上下文和压缩设置。

简单任务尽量少占资源；长对话、复杂分析和大量工具调用获得更大的上下文空间。如果一套设置经常失败、变慢或丢失前文，控制器会记录结果并逐步换用更合适的方案。

> 这是实验项目，不是 OpenAI 官方功能。它只修改本机 Codex 配置，不会读取账号密码，也不会更换模型或权限。

## 它能带来什么

- 根据当前对话长度、增长速度、工具调用量和压缩次数，在 48K–128K 上下文档位间选择；
- 动态改变“什么时候压缩”，避免太早压缩，也避免无限堆积；
- 每两分钟检查一次，记录失败、延迟、Token 和遗忘迹象；
- 为接入了请求包装器的程序提供逐请求预算、上下文筛选和反馈学习；
- 保留旧配置备份，支持查看状态和卸载。

## 先说明一个限制

自动安装后，**Codex 本机配置的上下文窗口、压缩点、推理强度和输出详细度会被调节**。但是，仓库中的“逐请求上下文筛选”和“每次请求后立即学习”需要上游程序通过 `--execute-request` 接入；普通 Codex 客户端目前不会自动把每个请求交给这个外部脚本。

| 功能 | 安装后是否自动生效 |
|---|---|
| 调整 Codex 上下文窗口和压缩点 | 是 |
| 定期分析本机会话并选择档位 | 是 |
| 防止旧窗口会话污染新策略 | 是 |
| 对任意外部 AI 请求逐次学习 | 需要接入请求包装器 |
| 自动删减每次 Codex 请求的上下文片段 | 暂不直接接管 Codex 请求链路 |

## 新电脑安装

需要 macOS 或带 systemd 的 Linux，以及 Python 3 和 Git。

```bash
git clone https://github.com/wzs2004/codex-elastic-budget-controller.git
cd codex-elastic-budget-controller
./scripts/install.sh --fresh-state
```

安装器会备份已有配置，安装最新版控制器和策略，并设置每 120 秒自动检查。备份保存在 `~/.codex/elastic-budget-backups/`。

安装完成后，**新建一个 Codex 对话**。已经打开的旧对话仍带有创建时的窗口信息，会被控制器保护性隔离。

### 更新

```bash
cd codex-elastic-budget-controller
git pull
./scripts/install.sh
```

普通更新保留学习状态。只有希望清空历史、重新学习时才使用 `--fresh-state`。

### 查看是否生效

```bash
./scripts/status.sh
```

看到“自动运行：已启用”和“当前对话：可参与弹性评估”即表示进入工作状态。如果显示“旧对话：已隔离”，新建一个 Codex 对话即可。

### 卸载

```bash
./scripts/uninstall.sh
```

卸载器不会擅自回滚 `config.toml`。如需恢复安装前配置，请从备份目录复制对应文件。

## 普通用户怎么看结果

- `economy`：短任务，优先节省；
- `balanced`：日常使用；
- `standard`：较长或较复杂的任务；
- `extended`：长对话、研究或高工具调用任务。

“自动压缩点”表示对话正文大约增长到多少 Token 后开始压缩。它不是越大越好：过小容易忘记前文，过大则增加成本和延迟。

## 开发者说明

下面保留实现细节，普通使用者不需要配置这些内容。

## 可调整参数

- `model_context_window`
- `model_auto_compact_token_limit`
- `model_reasoning_effort`
- `model_verbosity`
- `model_reasoning_summary`

控制器不会自动修改模型、模型提供商、服务等级、权限或工具开关。

## 核心策略

- 每个请求执行 preflight，综合输入长度、复杂度、质量风险、工具量、轮数和缓存复用性；
- 输出推理强度、详细度、输出上限、上下文选择、压缩模式、窗口、压缩阈值和缓存策略；
- `--execute-request` 在请求后自动解析反馈并更新带置信闸门的全协方差 LinUCB；漂移统计按 tier 隔离并衰减；
- 安全探索限制候选 profile，并用质量下界、失败率和平均延迟三重闸门剔除危险动作；
- 接收 `context_segments` 时，按查询相关度、结构、时序与保护标记执行确定性上下文筛选；
- 输出稳定前缀/动态后缀布局，便于上游复用 prompt cache；
- 可显式启用质量/失败级联：低成本动作未达质量线时自动重试更强 profile；
- 记录 propensity，并提供 IPS、SNIPS、Doubly Robust 离线策略评估；
- economy、balanced、standard、extended 四级容量策略；
- 在安全范围内连续调整压缩阈值；
- 保留最小上下文余量，并量化阈值以减少配置抖动；
- 使用迟滞区间、升降级冷却和每会话最多一次档位变化；
- 检测重复压缩并优先升级容量；
- 并发会话分别记录其策略，防止错误归因；
- 使用 UCB 探索/利用算法，根据成本代理值、延迟、失败、遗忘信号、完成情况和压缩次数持续学习；
- 只有已完成且静默达到设定时间的会话才进入学习，避免把仍在运行的会话误判为结束。

## 手动执行

```bash
python3 ~/.codex/elastic-budget-controller.py --force --verbose
```

控制器使用原子写入更新配置与状态，重复运行应保持幂等。

每请求预算预览：

```bash
python3 elastic-budget-controller.py --policy elastic-budget-policy.example.json \
  --request-json '{"estimated_input_tokens":16000,"task_type":"analysis","complexity":0.6,"quality_risk":0.6,"expected_tool_calls":3}'
```

闭环执行要求被包装命令在最后一行输出 JSON，至少包含 `success`；建议同时返回 `quality_score` 和 `usage`：

```bash
python3 elastic-budget-controller.py --policy elastic-budget-policy.example.json \
  --state /tmp/elastic-state.json --execute-request \
  '{"estimated_input_tokens":4000,"task_type":"rewrite","command":["python3","worker.py"]}'
```

没有明确 grader 时，控制器只能使用成功/失败，不能可靠学习语义质量。
预算计划会作为 `ELASTIC_BUDGET_PLAN` 传给被包装命令；每次运行的决策、propensity 和结果会追加到状态文件旁的 `*.requests.jsonl`，用于离线回放与反事实评估。

上下文筛选输入示例：

```json
{"text":"查找营收证据","context_segments":[
  {"text":"只能依据证据回答","role":"instruction","stable":true},
  {"text":"无关材料"},
  {"text":"营收增长 12%","must_keep":true}
]}
```

`context_selection.selected_segments` 是实际应交给模型的片段，`prompt_layout` 给出缓存友好的排序计划。控制器只生成计划；上游 worker 必须按计划组装请求。

启用级联时在请求 JSON 中设置 `"enable_cascade": true`。该功能会产生第二次模型调用及额外费用，因此默认只规划、不自动重试。

离线评估：

```bash
python3 benchmarks/off_policy_eval.py path/to/requests.jsonl
```

## 测试

```bash
python3 test_elastic_budget_controller.py
python3 test_off_policy_eval.py
python3 benchmarks/compare_v13.py
python3 benchmarks/compare_v14.py
python3 benchmarks/run_ab.py --help
```

评测方法、原始数据和结论见 [`benchmarks/README.zh-CN.md`](benchmarks/README.zh-CN.md)。

### 实验图表与问题分析

![A/B 实验总览](benchmarks/results/2026-09-25/charts/overview.svg)

- [正式实验报告](benchmarks/results/2026-09-25/REPORT.zh-CN.md)
- [实验暴露的问题、文献依据与下一版方案](benchmarks/PROBLEMS_AND_NEXT_STEPS.zh-CN.md)
- [Token 效率研究与闭环学习方案](benchmarks/TOKEN_EFFICIENCY_RESEARCH.zh-CN.md)
- [在线学习模拟结果](benchmarks/results/2026-09-25/online-learning-simulation.svg)（只验证学习机制，不代表真实模型节省）
- [v1.3 机制测试](benchmarks/results/2026-09-25/v1.3-mechanism-benchmark.svg)：1,000 次确定性受控试验，输入片段减少 34.62%，证据保留率 100%。这只证明筛选逻辑按设计工作，不等于真实模型费用降低 34.62%，也不证明回答质量必然提高；
- [v1.4 研究与结果](benchmarks/V1.4_RESEARCH_AND_RESULTS.zh-CN.md) · [2,000 请求机制模拟](benchmarks/results/2026-09-26-v1.4/v14-policy-simulation.svg) · [真实 Codex A/B](benchmarks/results/2026-09-26-v1.4/real-ab/REPORT.zh-CN.md)；
- [v2 验证式模型级联：研究、算法与边界](benchmarks/V2_VERIFIED_PROGRESSIVE_INFERENCE.zh-CN.md) · [无控制 baseline 对比真实 Codex A/B](benchmarks/results/2026-09-26-v2/model-cascade-ab/REPORT.zh-CN.md)；
- [按任务图表](benchmarks/results/2026-09-25/charts/by-case.svg) · [配对差值图](benchmarks/results/2026-09-25/charts/paired-deltas.svg) · [有效性问题图](benchmarks/results/2026-09-25/charts/validity-threats.svg)

## 安全提示

- 首次运行前备份 `~/.codex/config.toml`；
- 安装脚本会自动保存备份；
- 示例策略只是实验基线，不代表所有任务的最优设置；
- 仓库不包含本机会话日志、状态文件、凭据或个人配置；
- Codex 配置项可能随版本变化，部署前请核对最新官方文档；
- 若模型提供商没有公开计价，评测只能报告 token 和成本代理值，不能声称真实美元费用。

## 许可证

MIT
