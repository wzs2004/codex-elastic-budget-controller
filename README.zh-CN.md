# Codex 弹性预算控制器

这是一个仅依赖 Python 标准库的实验性控制器。它会为**每个请求**生成多维预算决策，并在请求完成后根据质量、Token、延迟与失败反馈在线更新，而不是等到上下文接近 48K 才开始工作。

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
- `--execute-request` 在请求后自动解析反馈并更新对角 LinUCB；旧反馈按几何衰减；
- 安全探索限制候选 profile，并剔除失败率超过上限的动作；
- economy、balanced、standard、extended 四级容量策略；
- 在安全范围内连续调整压缩阈值；
- 保留最小上下文余量，并量化阈值以减少配置抖动；
- 使用迟滞区间、升降级冷却和每会话最多一次档位变化；
- 检测重复压缩并优先升级容量；
- 并发会话分别记录其策略，防止错误归因；
- 使用 UCB 探索/利用算法，根据成本代理值、延迟、失败、遗忘信号、完成情况和压缩次数持续学习；
- 只有已完成且静默达到设定时间的会话才进入学习，避免把仍在运行的会话误判为结束。

## 安装

```bash
mkdir -p ~/.codex
cp elastic-budget-controller.py ~/.codex/
cp elastic-budget-policy.example.json ~/.codex/elastic-budget-policy.json
python3 ~/.codex/elastic-budget-controller.py --dry-run --force --verbose
```

请先检查 dry-run 输出，再启用定时执行。脚本优先使用 `$CODEX_HOME`，否则使用 `~/.codex`。

## 执行

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

## 测试

```bash
python3 test_elastic_budget_controller.py
python3 benchmarks/run_ab.py --help
```

评测方法、原始数据和结论见 [`benchmarks/README.zh-CN.md`](benchmarks/README.zh-CN.md)。

### 实验图表与问题分析

![A/B 实验总览](benchmarks/results/2026-09-25/charts/overview.svg)

- [正式实验报告](benchmarks/results/2026-09-25/REPORT.zh-CN.md)
- [实验暴露的问题、文献依据与下一版方案](benchmarks/PROBLEMS_AND_NEXT_STEPS.zh-CN.md)
- [Token 效率研究与闭环学习方案](benchmarks/TOKEN_EFFICIENCY_RESEARCH.zh-CN.md)
- [在线学习模拟结果](benchmarks/results/2026-09-25/online-learning-simulation.svg)（只验证学习机制，不代表真实模型节省）
- [按任务图表](benchmarks/results/2026-09-25/charts/by-case.svg) · [配对差值图](benchmarks/results/2026-09-25/charts/paired-deltas.svg) · [有效性问题图](benchmarks/results/2026-09-25/charts/validity-threats.svg)

## 安全提示

- 首次运行前备份 `~/.codex/config.toml`；
- 示例策略只是实验基线，不代表所有任务的最优设置；
- 仓库不包含本机会话日志、状态文件、凭据或个人配置；
- Codex 配置项可能随版本变化，部署前请核对最新官方文档；
- 若模型提供商没有公开计价，评测只能报告 token 和成本代理值，不能声称真实美元费用。

## 许可证

MIT
