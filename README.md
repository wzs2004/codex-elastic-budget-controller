# Codex Elastic Budget Controller

An experimental, standard-library-only controller for adapting Codex context and compaction settings from observed workload pressure and session outcomes.

## What it adjusts

- `model_context_window`
- `model_auto_compact_token_limit`
- `model_reasoning_effort`
- `model_verbosity`
- `model_reasoning_summary`

It deliberately leaves the model, provider, service tier, permissions, and tools unchanged.

## Strategy

The controller combines:

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

## Test

```bash
python3 test_elastic_budget_controller.py
```

## Safety notes

- Back up `~/.codex/config.toml` before first use.
- Treat the supplied policy as an experimental baseline, not a universal optimum.
- Session logs remain local; this repository contains no logs, state, credentials, or personal configuration.
- Config keys may change across Codex versions. Validate against current official documentation before deployment.

## License

MIT
