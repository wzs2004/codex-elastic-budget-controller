# Codex 弹性预算控制器

这是一个仅依赖 Python 标准库的实验性控制器，用于根据任务压力与历史反馈，动态调整 Codex 的上下文、压缩阈值、推理强度、输出详细度和推理摘要。

## 可调整参数

- `model_context_window`
- `model_auto_compact_token_limit`
- `model_reasoning_effort`
- `model_verbosity`
- `model_reasoning_summary`

控制器不会自动修改模型、模型提供商、服务等级、权限或工具开关。

## 核心策略

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

## 测试

```bash
python3 test_elastic_budget_controller.py
python3 benchmarks/run_ab.py --help
```

评测方法、原始数据和结论见 [`benchmarks/README.zh-CN.md`](benchmarks/README.zh-CN.md)。

## 安全提示

- 首次运行前备份 `~/.codex/config.toml`；
- 示例策略只是实验基线，不代表所有任务的最优设置；
- 仓库不包含本机会话日志、状态文件、凭据或个人配置；
- Codex 配置项可能随版本变化，部署前请核对最新官方文档；
- 若模型提供商没有公开计价，评测只能报告 token 和成本代理值，不能声称真实美元费用。

## 许可证

MIT
