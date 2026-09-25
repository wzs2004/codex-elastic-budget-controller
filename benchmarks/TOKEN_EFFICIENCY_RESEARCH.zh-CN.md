# Token 效率研究与闭环学习方案

更新日期：2026-09-25

## 核心判断

“弹性”不应等同于“快到 48K 才提高压缩阈值”。每个请求都应先做预算决策，完成后再用质量、Token、延迟、失败和缓存反馈更新策略。长上下文压缩只是动作空间的一部分。

当前实现采用轻量的对角 LinUCB，而不是神经网络。早期在线样本少、反馈延迟且只有所选动作有标签；上下文 bandit 更容易解释、增量更新和施加安全约束。积累足够数据后可替换为神经 contextual bandit，但仍应保留质量闸门、失败率上限和离线回放。

## 可融合项目和方法

| 层面 | 项目/论文 | 可融合能力 | 本项目处理方式 |
|---|---|---|---|
| 在线学习 | [Vowpal Wabbit](https://github.com/VowpalWabbit/vowpal_wabbit)、[learn_to_pick](https://github.com/VowpalWabbit/learn_to_pick)、[COBA](https://github.com/VowpalWabbit/coba) | 请求上下文→动作→只观察所选动作奖励；在线更新与离线回放 | per-tier 对角 LinUCB、最小探索、失败率安全过滤和几何衰减 |
| 模型路由 | [RouteLLM](https://arxiv.org/abs/2406.18665)、[FrugalGPT](https://arxiv.org/abs/2305.05176) | 按输入选择强/弱模型或级联 | 预留上游 router 接口；不声称能切换未配置模型 |
| Prompt 压缩 | [LLMLingua](https://github.com/microsoft/LLMLingua)、[LLMLingua-2](https://arxiv.org/abs/2403.12968) | 目标 Token、问句感知、结构保留 | 中长任务输出 extractive/semantic 动作提示；真实接入需 compressor |
| 真实系统验证 | [Prompt Compression in the Wild](https://arxiv.org/abs/2604.02985) | 压缩收益取决于提示长度、压缩率和硬件；必须同时测端到端延迟、压缩率遵从和质量 | 不再假设“压缩越多越省”，把 latency/quality 纳入同一奖励和实验报告 |
| 检索与选择 | LongLLMLingua、RAG reranking | 先检索/重排，再压缩无关上下文 | selective、retrieve-and-rerank、hierarchical-memory 三档动作 |
| 前缀缓存 | [OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)、[vLLM APC](https://github.com/vllm-project/vllm/blob/main/docs/design/prefix_caching.md) | 稳定前缀、复用 KV、减少重复 prefill | 输出 stable-prefix/avoid-cache-write；缓存命中进入成本反馈 |
| KV 管理 | [Ada-KV](https://arxiv.org/abs/2407.11550)、[vLLM KV offload](https://github.com/vllm-project/vllm/blob/main/docs/features/kv_offloading_usage.md) | 按注意力头或请求分配预算、选择性卸载/加载 KV | 作为自托管 runtime 插件点，不由本脚本直接控制 |
| 动态计算 | Early exit / adaptive depth | 简单 Token 少算层、困难 Token 多算层 | 属于模型或 serving runtime 能力，只能集成支持它的后端 |
| 评测 | [OpenAI eval best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)、[Graders](https://developers.openai.com/api/docs/guides/graders)、Google Vertex AI Evaluation | 任务分层、边界样本、自动 grader | 质量必须来自明确 grader/参考答案，不能由控制器猜测 |

## 新的全请求闭环

1. 请求前提取输入 Token、任务类型、复杂度、质量风险、工具量、轮数、时延敏感度和前缀复用性。
2. 每次输出 profile、推理强度、详细度、输出上限、上下文选择、压缩模式、窗口、压缩阈值和缓存策略。
3. 每个 Tier 只探索允许的 profile；失败率超过阈值的动作退出候选集。
4. `--execute-request` 包装命令，解析最后一行 JSON，将质量、分类 Token、延迟和成功状态立即回写 learner。
5. 每次更新对旧统计做几何衰减，适应负载、模型和价格漂移。
6. 保存候选动作、propensity、特征和结果，之后使用 replay、IPS/DR 和独立保留集评估。

奖励为：质量收益 − Token 成本 − 延迟成本 − 失败惩罚。生产环境还应加入质量非劣界、失败率上限、P95 延迟上限和敏感任务最低容量。

## 为什么短任务也会调整

短任务不需要强行“压缩一次”来证明弹性。它们会调整低推理强度、低输出上限、前缀是否值得写缓存、上下文是否只保留必要片段。强行压缩几十或几百 Token 会增加计算和信息损失。弹性的正确含义是每次做预算决策，而不是每次执行同一种压缩。

## 已知边界

- 控制器不能直接改变闭源服务的 early-exit、KV 量化或 speculative decoding，只提供集成提示。
- LinUCB 依赖 grader 质量；错误标签会令学习器优化错误目标。
- 现有 18 次真实运行没有跨过压缩阈值，只证明评测链路，不证明压缩收益。
- 模拟只验证闭环能学习，不是实际 LLM Token/费用证据。
- 上线前仍需 propensity 日志、保留集、漂移监控和回滚机制。
