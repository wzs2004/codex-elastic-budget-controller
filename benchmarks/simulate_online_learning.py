#!/usr/bin/env python3
"""Deterministic closed-loop simulation; not real-model evidence."""

import argparse
import importlib.util
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("elastic_budget", ROOT / "elastic-budget-controller.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

PROFILE_LEVEL = {"economy": 0, "balanced": 1, "standard": 2, "extended": 3}
TIER_LEVEL = {"micro": 0, "short": 1, "medium": 2, "long": 3, "ultra": 3}
REQUESTS = [
    {"estimated_input_tokens": 400, "task_type": "classification", "complexity": 0.12, "quality_risk": 0.10},
    {"estimated_input_tokens": 4000, "task_type": "rewrite", "complexity": 0.30, "quality_risk": 0.25},
    {"estimated_input_tokens": 16000, "task_type": "analysis", "complexity": 0.58, "quality_risk": 0.55, "expected_tool_calls": 3},
    {"estimated_input_tokens": 52000, "task_type": "coding", "complexity": 0.76, "quality_risk": 0.72, "expected_tool_calls": 8, "expected_turns": 5},
    {"estimated_input_tokens": 110000, "task_type": "research", "complexity": 0.92, "quality_risk": 0.90, "expected_tool_calls": 15, "expected_turns": 10},
]


def observation(tier, profile, request, rng):
    capacity, needed = PROFILE_LEVEL[profile], TIER_LEVEL[tier]
    shortfall, excess = max(0, needed - capacity), max(0, capacity - needed)
    quality = max(0.0, min(1.0, 0.97 - 0.24 * shortfall - 0.015 * excess + rng.uniform(-0.015, 0.015)))
    cost = request["estimated_input_tokens"] + 600 + 1300 * capacity
    latency = 2.0 + 1.8 * capacity + request["estimated_input_tokens"] / 12000
    return quality, cost, latency, shortfall < 2


def reward(quality, cost, latency, success):
    return quality - cost / 50000 - latency / 120 - (0 if success else 1.0)


def write_svg(path, adaptive, fixed):
    width, height, pad = 1000, 480, 64
    values = adaptive + fixed
    minimum, maximum = min(values), max(values)
    span = max(0.1, maximum - minimum)
    def points(items):
        return " ".join(
            f"{pad + index * (width - 2 * pad) / max(1, len(items) - 1):.1f},"
            f"{height - pad - (value - minimum) / span * (height - 2 * pad):.1f}"
            for index, value in enumerate(items)
        )
    path.write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="#fff"/><text x="{pad}" y="32" font-family="sans-serif" font-size="22" font-weight="700">Closed-loop learner simulation</text>
<line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#999"/><line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height-pad}" stroke="#999"/>
<polyline fill="none" stroke="#2563eb" stroke-width="4" points="{points(adaptive)}"/><polyline fill="none" stroke="#dc2626" stroke-width="3" stroke-dasharray="8 6" points="{points(fixed)}"/>
<text x="{pad+12}" y="{pad+18}" font-family="sans-serif" fill="#2563eb">adaptive diagonal LinUCB</text><text x="{pad+250}" y="{pad+18}" font-family="sans-serif" fill="#dc2626">fixed standard</text>
<text x="{width/2-70}" y="{height-18}" font-family="sans-serif">completed requests</text><text transform="translate(18 {height/2+60}) rotate(-90)" font-family="sans-serif">running mean reward</text>
</svg>''')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--output", default=str(ROOT / "benchmarks/results/2026-09-25/online-learning-simulation.json"))
    args = parser.parse_args()
    policy = json.loads((ROOT / "elastic-budget-policy.example.json").read_text())
    rng, state = random.Random(20260925), {}
    adaptive_curve, fixed_curve = [], []
    adaptive_total = fixed_total = 0.0
    for index in range(args.requests):
        request = dict(REQUESTS[index % len(REQUESTS)])
        plan = MODULE.request_budget(request, policy, state.get("request_learning") or {})
        tier, profile = plan["tier"], plan["budget_actions"]["profile"]
        quality, cost, latency, success = observation(tier, profile, request, rng)
        adaptive_total += reward(quality, cost, latency, success)
        state = MODULE.update_request_learning(state, {
            "tier": tier, "profile": profile, "features": plan["features"],
            "quality_score": quality, "cost_tokens": cost,
            "latency_seconds": latency, "success": success,
        }, policy)
        values = observation(tier, "standard", request, rng)
        fixed_total += reward(*values)
        adaptive_curve.append(adaptive_total / (index + 1))
        fixed_curve.append(fixed_total / (index + 1))
    result = {
        "kind": "deterministic_simulation_not_real_model_evidence",
        "requests": args.requests,
        "adaptive_mean_reward": round(adaptive_total / args.requests, 6),
        "fixed_standard_mean_reward": round(fixed_total / args.requests, 6),
        "learner_arms": state["request_learning"]["arms"],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    write_svg(output.with_suffix(".svg"), adaptive_curve, fixed_curve)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
