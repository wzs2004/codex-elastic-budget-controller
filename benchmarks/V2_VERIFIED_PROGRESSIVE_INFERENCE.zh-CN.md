# v2：验证式渐进推理（Verified Progressive Inference）

## 结论先行

v1.4 的真实 A/B 没有证明成本下降：五个 adaptive 请求全部落到同一 `balanced` 档，平均 token 与延迟反而上升。问题不只是学习器参数，而是核心假设不可靠——在生成答案前预测“这次需要多少算力”，很难同时识别任务难度、模型偶然失败和输出格式错误。

v2 因而不再把 bandit 路由作为主控制环，而改为：

1. 用独立的低成本模型与低计算档生成候选答案；
2. 用模型外部、确定性、可执行的质量契约验收；
3. 验收通过立即停止；
4. 只有失败才升级到标准档；
5. 没有可信 verifier 的任务不宣称可安全节省，直接使用标准档。

这是一种从“事前猜预算”转向“事后验证后停止”的范式变化。它把节省建立在可观察的正确性证据上，而不是模型 confidence、自评或任务标签。

## 文献与专业书籍给出的共同方向

| 来源 | 关键思想 | v2 取舍 |
|---|---|---|
| [FrugalGPT](https://arxiv.org/abs/2305.05176) | 级联能形成成本—质量前沿 | 保留低成本先行和按需升级，但不使用模型自信作为接受条件 |
| [Language Model Cascades](https://arxiv.org/abs/2404.10136) | 生成式序列置信度有长度偏差，token 级规则优于朴素序列置信度 | 不依赖生成概率，改用任务外部 verifier |
| [Learning to Defer](https://proceedings.mlr.press/v119/mozannar20b.html) | 预测器应能拒绝并交给更可靠的专家 | 契约失败即拒绝 economy，交给 standard |
| [PonderNet](https://arxiv.org/abs/2107.05407) | 计算量应随问题复杂度动态停止 | 将 learned halting 改造成工程上可部署的 verifier-gated halting |
| [Using Anytime Algorithms in Intelligent Systems](https://doi.org/10.1609/aimag.v17i3.1232) 与 [Metareasoning](https://mitpress.mit.edu/9780262538756/metareasoning/) | 先给出可用解，再按价值决定是否继续计算 | economy 是初始解，契约决定追加计算是否有价值 |
| [Conformal Risk Control](https://arxiv.org/abs/2208.02814) | 用校准数据控制一般单调损失 | 当前实现只提供确定性契约；未来用留出集校准升级阈值与风险覆盖率 |
| [LEVER](https://arxiv.org/abs/2302.08468) | 执行结果可作为独立验证信号改进生成 | 采用 JSON/单测/命令结果等可执行反馈，而非让同一模型口头自评 |
| [Self-Consistency](https://arxiv.org/abs/2203.11171) | 多路径采样能提高推理质量 | 不作为默认方案：它先支付多次生成成本，只在 verifier 无法区分候选时才值得使用 |
| [Algorithms for Decision Making](https://mitpress.mit.edu/9780262370233/algorithms-for-decision-making/) | 明确效用、风险、观测与决策规则 | 将“接受/升级”建模为有证据的顺序决策，而不是单一成本奖励最大化 |
| [Prediction, Learning, and Games](https://www.cambridge.org/core/books/prediction-learning-and-games/9E1757F220A1C15C633B2592ACCFBE5C) | 在线决策必须面对反馈、遗憾与非平稳性 | 旧学习器保留为实验组件，但不再承担质量安全闸门 |
| [OpenAI eval 最佳实践](https://developers.openai.com/api/docs/guides/evaluation-best-practices) | 任务特定评测、自动评分、完整日志并与人工判断校准 | 配对 A/B、结构化答案、原始事件、逐阶段用量和明确适用边界 |

## 算法

```text
candidate = run(cheap_model, economy_profile)
if deterministic_contract(candidate) == PASS:
    return candidate

candidate = run(standard_model, standard_profile)
if deterministic_contract(candidate) == PASS:
    return candidate

return explicit_failure
```

当前实现的契约是 `json_subset`：参考对象中的所有字段、嵌套数组和值都必须精确匹配；候选答案允许包含额外字段。验证器运行在模型调用之外，模型看不到参考答案。每个阶段的 token、延迟、原始事件和答案分别保存，升级成本不会被隐藏。

## 与 v1.4 的关系

- 保留：profile 配置、执行器、超时处理、用量采集、成本代理、原始证据、配对交替 A/B 和图表。
- 降级为兼容组件：LinUCB、冷启动路由、漂移检测和基于模型结果的 cascade。
- 新主路径：`quality_contract_plan()` 与 `verify_quality_contract()`；契约模式不更新旧 bandit，避免用 oracle 反馈污染线上学习状态。

## 保证边界

“契约通过”不等于“答案在所有意义上正确”。保证只覆盖契约表达的性质，并依赖参考答案或 verifier 本身正确。当前 benchmark 的 oracle 很强，适用于算术、约束求解、结构化分析和可用单测验证的代码任务；它不适用于开放式写作、审美判断、未定义事实核查或需求本身含糊的任务。

生产化需要建立 verifier registry，例如 JSON 约束、数值容差、数据库不变量、编译器、单元测试、静态分析、求解器和人工复核。引入学习式 verifier 时，还必须用独立留出集测量假阳性，并可进一步使用 conformal risk control 校准接受阈值；在完成校准前不能宣称有限样本质量保证。

## 实验设计

- baseline：固定 `standard`，每个任务只调用一次，代表完全不使用成本控制。
- v2：固定 `cheap model/economy → verifier → standard model`；只有契约失败才发生第二次调用。
- 两组使用同一 provider、prompt、JSON schema 和工具权限；baseline 使用本机标准模型，v2 廉价阶段默认为 `gpt-6-sol`，顺序交替。
- 质量按隐藏的结构化参考答案评分；参考答案只在模型返回后交给 harness。
- 无公开 provider 单价时只报告 token、加权成本代理和延迟，不伪造货币费用。

首轮同模型负结果见 [`results/2026-09-26-v2/real-ab-same-model/REPORT.zh-CN.md`](results/2026-09-26-v2/real-ab-same-model/REPORT.zh-CN.md)；真正模型级联结果见 [`results/2026-09-26-v2/model-cascade-ab/REPORT.zh-CN.md`](results/2026-09-26-v2/model-cascade-ab/REPORT.zh-CN.md)。

## 真实模型级联结果

5 个任务的 baseline 与 v2 都达到契约质量 1.000。v2 有 4/5 个廉价候选直接通过，只有 1/5 升级，因此标准模型调用从 5 次降至 1 次（减少 80%）。但本轮平均总 token 从 14,960.8 增至 18,688.2（+24.91%），加权 token 代理从 13,293.8 增至 19,593.0（+47.38%），延迟从 11.042 秒增至 28.792 秒（+160.75%）。

按分阶段 token 做简化价格敏感性分析，廉价模型单位 token 价格必须低于标准模型的报告所示盈亏平衡比例，v2 才可能在本样本产生货币优势。由于 provider 价格未知，项目不把“减少标准模型调用”偷换成“已经省钱”。这次实验支持的是质量门控和昂贵调用削减机制，而不是普遍 token 或延迟节省；更大样本、随机化运行顺序和真实账单价格仍是生产结论的必要条件。
