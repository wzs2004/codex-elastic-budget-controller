#!/usr/bin/env python3
"""Evaluate a target routing policy from logged contextual-bandit JSONL."""

import argparse
import json
import math
from pathlib import Path


def evaluate(rows, target_key="target_probability"):
    valid = []
    for row in rows:
        propensity = float(row.get("action_propensity") or row.get("propensity")
                           or (row.get("plan") or {}).get("action_propensity") or 0)
        target = float(row.get(target_key) or 0)
        reward = float(row.get("reward") if row.get("reward") is not None
                       else (row.get("feedback") or {}).get("reward", 0))
        prediction = float(row.get("reward_prediction") or reward)
        target_prediction = float(row.get("target_reward_prediction") or prediction)
        if propensity > 0 and target >= 0:
            valid.append((target / propensity, reward, prediction, target_prediction))
    if not valid:
        raise ValueError("no valid rows with positive propensity")
    ips = sum(weight * reward for weight, reward, _, _ in valid) / len(valid)
    weight_sum = sum(weight for weight, _, _, _ in valid)
    snips = sum(weight * reward for weight, reward, _, _ in valid) / max(weight_sum, 1e-12)
    dr_values = [target_prediction + weight * (reward - prediction)
                 for weight, reward, prediction, target_prediction in valid]
    dr = sum(dr_values) / len(dr_values)
    effective_sample_size = weight_sum ** 2 / max(1e-12, sum(weight ** 2 for weight, *_ in valid))
    return {"rows": len(valid), "ips": round(ips, 8), "snips": round(snips, 8),
            "doubly_robust": round(dr, 8),
            "effective_sample_size": round(effective_sample_size, 3),
            "max_importance_weight": round(max(weight for weight, *_ in valid), 6)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--target-key", default="target_probability")
    args = parser.parse_args()
    rows = [json.loads(line) for line in Path(args.log).read_text().splitlines() if line.strip()]
    print(json.dumps(evaluate(rows, args.target_key), indent=2))


if __name__ == "__main__":
    main()
