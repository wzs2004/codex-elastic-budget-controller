# Codex Elastic Budget Controller

[简体中文](README.zh-CN.md)

An experimental, standard-library-only controller that makes a multi-dimensional budget decision for every request and learns online from quality, token, latency, and failure feedback. It no longer waits for a 48K context threshold before becoming adaptive.

## What it adjusts

- `model_context_window`
- `model_auto_compact_token_limit`
- `model_reasoning_effort`
- `model_verbosity`
- `model_reasoning_summary`

It deliberately leaves the model, provider, service tier, permissions, and tools unchanged.

## Strategy

The controller combines:

- per-request preflight planning across reasoning, output, context selection, compression, and cache policy;
- automatic post-request feedback and a safe diagonal LinUCB learner with drift decay;
- query-aware extractive context selection with protected boundaries;
- stable-prefix/dynamic-suffix prompt layout hints for cache reuse;
- quality/failure cascade fallback, enabled only by explicit request opt-in;
- quality, failure-rate, and latency guardrails plus IPS/SNIPS/DR evaluation;
- four capacity profiles from economy through extended;
- an elastic compaction threshold with reserve and quantization limits;
- hysteresis, cooldowns, legacy-session isolation, and one profile change per session;
- repeat-compaction escalation;
- per-session strategy attribution;
- UCB exploration/exploitation using normalized cost, latency, failures, forgetting signals, completion, and compaction feedback.

Concurrent sessions are handled conservatively: a recent preferred session is not displaced merely because another JSONL file has a newer modification time. Learning occurs only after a completed session becomes stale.

## Install

```bash
mkdir -p ~/.codex
cp elastic-budget-controller.py ~/.codex/
cp elastic-budget-policy.example.json ~/.codex/elastic-budget-policy.json
python3 ~/.codex/elastic-budget-controller.py --dry-run --force --verbose
```

Review the dry-run output before enabling scheduled execution. The script defaults to `$CODEX_HOME` when set, otherwise `~/.codex`.

## Run

```bash
python3 ~/.codex/elastic-budget-controller.py --force --verbose
```

For periodic execution, use your platform scheduler. The controller writes config and state atomically and is designed to be idempotent.

`--execute-request` exports the selected plan as `ELASTIC_BUDGET_PLAN`, parses the worker's final JSON line, updates the learner immediately, and appends a decision/propensity/outcome record beside the state file for offline evaluation.

When `context_segments` are supplied, the plan contains the selected segments and a cache-friendly layout. The worker must apply that plan when assembling the model request. Set `"enable_cascade": true` only when an extra fallback call and its cost are acceptable.

## Test

```bash
python3 test_elastic_budget_controller.py
python3 test_off_policy_eval.py
python3 benchmarks/compare_v13.py
python3 benchmarks/run_ab.py --help
```

See [`benchmarks/README.zh-CN.md`](benchmarks/README.zh-CN.md) for the reproducible paired A/B methodology, raw evidence format, limitations, and results.

### Evidence charts and failure analysis

![A/B benchmark overview](benchmarks/results/2026-09-25/charts/overview.svg)

- [Formal experiment report (Chinese)](benchmarks/results/2026-09-25/REPORT.zh-CN.md)
- [Problems, literature review, and next experiment design (Chinese)](benchmarks/PROBLEMS_AND_NEXT_STEPS.zh-CN.md)
- [Token-efficiency research and closed-loop design (Chinese)](benchmarks/TOKEN_EFFICIENCY_RESEARCH.zh-CN.md)
- [Online-learning simulation](benchmarks/results/2026-09-25/online-learning-simulation.svg) (mechanism validation only, not real-model savings)
- [v1.3 mechanism benchmark](benchmarks/results/2026-09-25/v1.3-mechanism-benchmark.svg): 1,000 deterministic trials, 34.62% fewer selected input tokens and 100% evidence retention; not real-model evidence
- [By-case chart](benchmarks/results/2026-09-25/charts/by-case.svg) · [Paired deltas](benchmarks/results/2026-09-25/charts/paired-deltas.svg) · [Validity threats](benchmarks/results/2026-09-25/charts/validity-threats.svg)

## Safety notes

- Back up `~/.codex/config.toml` before first use.
- Treat the supplied policy as an experimental baseline, not a universal optimum.
- Session logs remain local; this repository contains no logs, state, credentials, or personal configuration.
- Config keys may change across Codex versions. Validate against current official documentation before deployment.

## License

MIT
