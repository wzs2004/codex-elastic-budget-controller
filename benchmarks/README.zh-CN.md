# A/B 评测方法

本评测用于比较固定配置（baseline）与弹性策略（adaptive），不是比较不同模型。

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

- 同一模型和 provider；
- 同一提示词、输入文件、沙箱、工具权限和输出 JSON Schema；
- 每个 case 在 baseline/adaptive 下各运行相同轮数；
- baseline 固定为 standard 参数；
- adaptive 根据预先声明且写入数据集的任务压力档位选择策略，不能根据答案临时选择；
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
- adaptive 的任务压力档位在此评测中预先标注，生产环境则由实时指标和历史学习共同决定。

## 复现

```bash
python3 benchmarks/run_ab.py --rounds 3 --output benchmarks/results/latest
python3 benchmarks/generate_charts.py benchmarks/results/latest
```

脚本调用本机 `codex exec`，因此需要已经可用的 Codex CLI 登录和配置。建议先用 `--rounds 1` 验证环境。

本轮结果的图表、有效性问题和改进方案见 [`PROBLEMS_AND_NEXT_STEPS.zh-CN.md`](PROBLEMS_AND_NEXT_STEPS.zh-CN.md)。
