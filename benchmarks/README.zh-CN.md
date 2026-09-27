# A/B 评测方法

最新版评测比较完全不做成本控制的固定标准模型 baseline，与 v2 验证式渐进推理：廉价模型先行，外部契约失败才升级标准模型。

## 方法来源

- OpenAI Evals：固定数据集、明确参考答案、自动 grader、逐条保存结果；
- Google Vertex AI Evaluation：同时报告 pointwise 分数、pairwise 胜率、均值和标准差；
- GitHub 上的 OpenAI Evals 与 Promptfoo：以可重复执行的配置和原始结果替代主观挑选案例。

参考资料：

- https://developers.openai.com/api/docs/guides/evaluation-best-practices
- https://developers.openai.com/api/docs/guides/graders
- https://developers.openai.com/api/docs/guides/graders
- https://cloud.google.com/vertex-ai/generative-ai/docs/models/eval-python-sdk/view-evaluation
- https://github.com/openai/evals
- https://github.com/promptfoo/promptfoo

## 控制变量

- 同一 provider；baseline 使用本机标准模型，v2 首阶段显式使用廉价模型，升级阶段回到同一标准模型；
- 同一提示词、输入文件、沙箱、工具权限和输出 JSON Schema；
- 每个 case 在 baseline/adaptive 下各运行相同轮数；
- baseline 固定为 standard 参数；
- v2 不做事前难度预测；只有模型返回后，隐藏参考答案驱动的确定性契约才能决定是否升级；
- 两组交替执行，降低时间顺序偏差；
- grader 只检查结构化参考答案，不使用主观人工挑选。

## 指标

- `quality_score`：参考答案字段的准确率，范围 0–1；
- `latency_seconds`：端到端执行时间；
- input、cached input、output、reasoning token；
- `cost_proxy`：与控制器相同的加权 token 指标，不等于货币费用；
- pairwise：同一 case/轮次下，质量更高者胜；质量相同时，成本代理值更低者胜；两者相同为平局；
- 汇总包含均值、标准差、胜率和相对变化。
- 超时或失败没有完整 usage 时，不能把 token/成本记成 0 并当作节省；资源均值只统计完成样本，同时报告完成数与超时数。

真实货币费用只有在运行时显式提供每百万 token 单价后才计算。未配置单价时必须显示 `null`，不得根据未知的自定义 provider 猜测价格。

## 局限

- 小样本只能作为回归证据，不能证明对所有任务普遍更优；
- 上下文阈值对未接近阈值的短任务影响有限；
- 模型输出存在随机性，因此保留逐轮结果并报告标准差；
- 自定义 provider 的缓存和计费规则可能与公开 API 不同；
- benchmark 的 oracle 契约强于一般生产 verifier；结果只适用于可自动验证任务。

## 工具输出压缩机制实验

使用固定的 pytest、构建日志、heartbeat、git 状态、JSON 和 ANSI 混合输出，比较原始输出与 `shrink_output()` 的抽取式压缩结果：

```bash
python3 benchmarks/benchmark_output_compaction.py
```

结果写入 `benchmarks/results/2026-09-26-output-compaction.json`，实验说明见 [`OUTPUT_COMPACTION_EXPERIMENT.zh-CN.md`](OUTPUT_COMPACTION_EXPERIMENT.zh-CN.md)。

## 复现

```bash
python3 benchmarks/benchmark_output_compaction.py
```

该命令使用固定样本复现实验，并更新 `benchmarks/results/2026-09-26-output-compaction.json`。
图表见 [`2026-09-26-output-compaction.svg`](results/2026-09-26-output-compaction.svg)，详细说明见 [`OUTPUT_COMPACTION_EXPERIMENT.zh-CN.md`](OUTPUT_COMPACTION_EXPERIMENT.zh-CN.md)。

需要真实 Codex A/B 时，可使用 `run_ab.py`；该路径依赖本机 `codex exec` 登录和配置，并不属于默认回归测试。

### 最新真实 A/B 结果

[`2026-09-27-real-ab/REPORT.zh-CN.md`](results/2026-09-27-real-ab/REPORT.zh-CN.md) 包含 5 类任务、3 轮配对、30 个真实 trial 的完整结果、原始证据和图表。该实验质量与成功率均为 100%，标准模型调用减少 73.33%，但总 token 增加 30.69%、成本代理增加 8.35%；因此不能把本版本宣传为已证明节省 token 或账单。
