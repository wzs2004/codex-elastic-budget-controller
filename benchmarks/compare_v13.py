#!/usr/bin/env python3
"""Deterministic controlled benchmark for v1.3 mechanisms, not model evidence."""

import importlib.util
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("controller", ROOT / "elastic-budget-controller.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
POLICY = json.loads((ROOT / "elastic-budget-policy.example.json").read_text())


def context_case(index):
    noise = [{"text": f"irrelevant filler block {number} weather sports", "tokens": 900}
             for number in range(10)]
    return {
        "text": f"find evidence for target-{index}", "estimated_input_tokens": 10000,
        "task_type": "analysis", "complexity": 0.6, "quality_risk": 0.7,
        "context_segments": [
            {"text": "Answer only from evidence.", "tokens": 250,
             "role": "instruction", "stable": True},
            *noise[:5],
            {"text": f"Evidence: target-{index} value is {index * 7}.", "tokens": 900},
            *noise[5:],
            {"text": "End of source.", "tokens": 250},
        ],
    }


def main():
    rng = random.Random(20260925)
    state = {}
    trials = []
    for index in range(1000):
        request = context_case(index % 20)
        plan = MODULE.request_budget(request, POLICY, state.get("request_learning") or {})
        selection = plan["context_selection"]
        retained = selection["selected_tokens"]
        evidence_kept = 6 in selection["selected_indices"]
        quality = 0.96 if evidence_kept else 0.25
        profile = plan["budget_actions"]["profile"]
        profile_cost = {"economy": 300, "balanced": 600, "standard": 1000, "extended": 1600}[profile]
        latency = 1.5 + retained / 8000 + profile_cost / 1000 + rng.uniform(-0.05, 0.05)
        baseline_tokens = sum(item["tokens"] for item in request["context_segments"])
        baseline_reward = 0.96 - baseline_tokens / 50000 - 4.0 / 120
        adaptive_reward = quality - (retained + profile_cost) / 50000 - latency / 120
        state = MODULE.update_request_learning(state, {
            "tier": plan["tier"], "profile": profile, "features": plan["features"],
            "quality_score": quality, "cost_tokens": retained + profile_cost,
            "latency_seconds": latency, "success": evidence_kept,
        }, POLICY)
        trials.append({"baseline_tokens": baseline_tokens, "adaptive_tokens": retained,
                       "evidence_kept": evidence_kept, "baseline_reward": baseline_reward,
                       "adaptive_reward": adaptive_reward})
    result = {
        "kind": "deterministic_mechanism_benchmark_not_real_model_evidence",
        "trials": len(trials),
        "mean_baseline_input_tokens": round(sum(x["baseline_tokens"] for x in trials) / len(trials), 2),
        "mean_adaptive_input_tokens": round(sum(x["adaptive_tokens"] for x in trials) / len(trials), 2),
        "input_token_reduction_percent": round(100 * (1 - sum(x["adaptive_tokens"] for x in trials) /
                                                       sum(x["baseline_tokens"] for x in trials)), 2),
        "evidence_retention_rate": round(sum(x["evidence_kept"] for x in trials) / len(trials), 4),
        "baseline_mean_reward": round(sum(x["baseline_reward"] for x in trials) / len(trials), 6),
        "adaptive_mean_reward": round(sum(x["adaptive_reward"] for x in trials) / len(trials), 6),
    }
    output = ROOT / "benchmarks" / "results" / "2026-09-25" / "v1.3-mechanism-benchmark.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    width, height = 900, 440
    token_values = [result["mean_baseline_input_tokens"], result["mean_adaptive_input_tokens"]]
    reward_values = [result["baseline_mean_reward"], result["adaptive_mean_reward"]]
    token_scale = 240 / max(token_values)
    reward_scale = 240 / max(reward_values)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#fff"/><text x="48" y="38" font-family="sans-serif" font-size="22" font-weight="700">v1.3 controlled mechanism benchmark (1,000 trials)</text>
<text x="48" y="78" font-family="sans-serif" font-size="16">Mean selected input tokens</text>
<rect x="80" y="110" width="120" height="{token_values[0]*token_scale:.1f}" fill="#94a3b8" transform="translate(0 {240-token_values[0]*token_scale:.1f})"/>
<rect x="240" y="110" width="120" height="{token_values[1]*token_scale:.1f}" fill="#2563eb" transform="translate(0 {240-token_values[1]*token_scale:.1f})"/>
<text x="90" y="374" font-family="sans-serif">baseline {token_values[0]:.0f}</text><text x="246" y="374" font-family="sans-serif">adaptive {token_values[1]:.0f}</text>
<text x="488" y="78" font-family="sans-serif" font-size="16">Mean reward</text>
<rect x="520" y="110" width="120" height="{reward_values[0]*reward_scale:.1f}" fill="#94a3b8" transform="translate(0 {240-reward_values[0]*reward_scale:.1f})"/>
<rect x="680" y="110" width="120" height="{reward_values[1]*reward_scale:.1f}" fill="#16a34a" transform="translate(0 {240-reward_values[1]*reward_scale:.1f})"/>
<text x="522" y="374" font-family="sans-serif">baseline {reward_values[0]:.3f}</text><text x="676" y="374" font-family="sans-serif">adaptive {reward_values[1]:.3f}</text>
<text x="48" y="414" font-family="sans-serif" fill="#475569">Token reduction {result['input_token_reduction_percent']}% · evidence retention {result['evidence_retention_rate']*100:.0f}% · deterministic simulation, not real-model evidence</text>
</svg>'''
    output.with_suffix(".svg").write_text(svg)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
