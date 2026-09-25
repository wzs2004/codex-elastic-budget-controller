#!/usr/bin/env python3
"""Seeded v1.4 policy simulation; mechanism evidence, not model evidence."""

import importlib.util
import json
import math
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("controller", ROOT / "elastic-budget-controller.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
POLICY = json.loads((ROOT / "elastic-budget-policy.example.json").read_text())
LEVEL = {"economy": 0, "balanced": 1, "standard": 2, "extended": 3}


def legacy_choose(tier, request, state):
    candidates = POLICY["request_budgeting"]["candidate_profiles"][tier]
    features = MODULE.request_feature_vector(MODULE.normalize_request(request))
    arms = state.setdefault("arms", {})
    under_tested = [name for name in candidates if arms.get(f"{tier}:{name}", {}).get("count", 0) < 2]
    if under_tested:
        return under_tested[0]
    def score(name):
        arm = arms.get(f"{tier}:{name}", {})
        diagonal = arm.get("a_diagonal", [1.0] * len(features))
        response = arm.get("b_vector", [0.0] * len(features))
        mean = sum(response[i] / max(diagonal[i], 1e-9) * features[i] for i in range(len(features)))
        uncertainty = math.sqrt(sum(features[i] ** 2 / max(diagonal[i], 1e-9) for i in range(len(features))))
        return mean + 0.35 * uncertainty
    return max(candidates, key=score)


def legacy_update(tier, profile, request, reward, state):
    features = MODULE.request_feature_vector(MODULE.normalize_request(request))
    arms = state.setdefault("arms", {})
    for key, value in arms.items():
        value["a_diagonal"] = [1 + (x - 1) * 0.98 for x in value["a_diagonal"]]
        value["b_vector"] = [x * 0.98 for x in value["b_vector"]]
    key = f"{tier}:{profile}"
    arm = arms.setdefault(key, {"count": 0, "a_diagonal": [1.0] * len(features),
                                "b_vector": [0.0] * len(features)})
    arm["count"] += 1
    arm["a_diagonal"] = [arm["a_diagonal"][i] + features[i] ** 2 for i in range(len(features))]
    arm["b_vector"] = [arm["b_vector"][i] + reward * features[i] for i in range(len(features))]


def required_level(request, drifted):
    complexity = request["complexity"]
    risk = request["quality_risk"]
    token = math.log1p(request["estimated_input_tokens"]) / math.log(131073)
    interaction = complexity * risk + 0.45 * token * risk
    level = 0 if interaction < 0.18 else 1 if interaction < 0.43 else 2 if interaction < 0.72 else 3
    if drifted and request["task_type"] in {"coding", "research"}:
        level = min(3, level + 1)
    return level


def observe(request, profile, noise, drifted):
    chosen, needed = LEVEL[profile], required_level(request, drifted)
    shortfall, excess = max(0, needed - chosen), max(0, chosen - needed)
    quality = MODULE.clamp(0.965 - 0.22 * shortfall - 0.012 * excess + noise)
    failure = shortfall >= 2
    cost = request["estimated_input_tokens"] + 550 + 1250 * chosen
    latency = 1.8 + request["estimated_input_tokens"] / 15000 + 1.55 * chosen
    reward = quality - cost / 50000 - latency / 120 - (1.0 if failure else 0.0)
    return quality, cost, latency, not failure, reward


def requests(rng, count):
    task_types = ["classification", "rewrite", "analysis", "coding", "research"]
    token_bases = [350, 3500, 14000, 48000, 105000]
    rows = []
    for index in range(count):
        group = index % len(task_types)
        complexity = MODULE.clamp([0.12, 0.28, 0.55, 0.75, 0.9][group] + rng.uniform(-0.12, 0.12))
        risk = MODULE.clamp(0.15 + 0.72 * complexity + rng.uniform(-0.2, 0.2))
        rows.append({
            "estimated_input_tokens": max(64, int(token_bases[group] * rng.uniform(0.72, 1.28))),
            "task_type": task_types[group], "complexity": complexity, "quality_risk": risk,
            "expected_tool_calls": max(0, group * 2 + rng.randrange(3) - 1),
            "expected_turns": max(1, group * 2 + rng.randrange(3)),
            "latency_sensitive": group < 2, "reusable_prefix": group > 1,
        })
    return rows


def summarize(records):
    count = len(records)
    return {
        "mean_quality": round(sum(x["quality"] for x in records) / count, 6),
        "mean_cost_tokens": round(sum(x["cost"] for x in records) / count, 2),
        "mean_latency_seconds": round(sum(x["latency"] for x in records) / count, 4),
        "failure_rate": round(1 - sum(x["success"] for x in records) / count, 6),
        "mean_reward": round(sum(x["reward"] for x in records) / count, 6),
        "profile_counts": dict(Counter(x["profile"] for x in records)),
    }


def write_svg(path, result):
    metrics = [("Mean reward", "mean_reward", True), ("Quality", "mean_quality", True),
               ("Cost tokens", "mean_cost_tokens", False), ("Failure rate", "failure_rate", False)]
    methods = ["fixed_standard", "legacy_diagonal", "v14_full_covariance"]
    colors = {"fixed_standard": "#64748b", "legacy_diagonal": "#f59e0b", "v14_full_covariance": "#2563eb"}
    width, height = 1180, 640
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="#fff"/>',
             '<text x="44" y="42" font-family="sans-serif" font-size="24" font-weight="700">v1.4 controlled policy simulation</text>',
             '<text x="44" y="68" font-family="sans-serif" font-size="13" fill="#64748b">Seeded correlated workloads with an abrupt halfway drift; lower cost/failure is better</text>']
    for panel, (label, key, higher) in enumerate(metrics):
        x0 = 44 + (panel % 2) * 560; y0 = 112 + (panel // 2) * 255
        values = [result[method][key] for method in methods]
        floor = min(0, min(values)) if higher else 0
        ceiling = max(values) * 1.12 or 1
        span = ceiling - floor
        parts.append(f'<text x="{x0}" y="{y0}" font-family="sans-serif" font-size="16" font-weight="700">{label}</text>')
        for index, method in enumerate(methods):
            value = result[method][key]
            y = y0 + 32 + index * 52
            bar = max(2, (value - floor) / span * 360)
            parts.append(f'<text x="{x0}" y="{y+16}" font-family="sans-serif" font-size="12">{method}</text>')
            parts.append(f'<rect x="{x0+170}" y="{y}" width="360" height="22" rx="5" fill="#f1f5f9"/>')
            parts.append(f'<rect x="{x0+170}" y="{y}" width="{bar:.1f}" height="22" rx="5" fill="{colors[method]}"/>')
            shown = f'{value:.4f}' if abs(value) < 10 else f'{value:,.0f}'
            parts.append(f'<text x="{x0+178+bar}" y="{y+16}" font-family="sans-serif" font-size="11">{shown}</text>')
    parts.append('<text x="44" y="616" font-family="sans-serif" font-size="12" fill="#64748b">Mechanism simulation only. It does not estimate real model quality or monetary cost.</text></svg>')
    path.write_text("\n".join(parts) + "\n")


def main():
    rng = random.Random(20260926)
    workload = requests(rng, 2000)
    noises = [rng.uniform(-0.018, 0.018) for _ in workload]
    legacy_state, modern_state = {}, {}
    records = {"fixed_standard": [], "legacy_diagonal": [], "v14_full_covariance": []}
    for index, (request, noise) in enumerate(zip(workload, noises)):
        drifted = index >= len(workload) // 2
        base_plan = MODULE.request_budget(request, POLICY, {})
        tier = base_plan["tier"]
        legacy = legacy_choose(tier, request, legacy_state)
        modern_plan = MODULE.request_budget(request, POLICY, modern_state.get("request_learning") or {})
        modern = modern_plan["budget_actions"]["profile"]
        for method, profile in (("fixed_standard", "standard"), ("legacy_diagonal", legacy),
                                ("v14_full_covariance", modern)):
            quality, cost, latency, success, value = observe(request, profile, noise, drifted)
            records[method].append({"profile": profile, "quality": quality, "cost": cost,
                                    "latency": latency, "success": success, "reward": value})
            if method == "legacy_diagonal":
                legacy_update(tier, profile, request, value, legacy_state)
            elif method == "v14_full_covariance":
                modern_state = MODULE.update_request_learning(modern_state, {
                    "tier": tier, "profile": profile, "features": modern_plan["features"],
                    "quality_score": quality, "cost_tokens": cost,
                    "latency_seconds": latency, "success": success,
                }, POLICY)
    result = {
        "kind": "seeded_mechanism_simulation_not_real_model_evidence", "requests": len(workload),
        "drift_at_request": len(workload) // 2,
        **{method: summarize(values) for method, values in records.items()},
        "v14_drift_detections": sum(
            item.get("detections", 0)
            for item in modern_state["request_learning"].get("drifts", {}).values()
        ),
    }
    output = ROOT / "benchmarks/results/2026-09-26-v1.4/v14-policy-simulation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    write_svg(output.with_suffix(".svg"), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
