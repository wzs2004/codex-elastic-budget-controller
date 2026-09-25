#!/usr/bin/env python3
"""Run reproducible paired Codex CLI A/B trials and preserve raw evidence."""

import argparse
import importlib.util
import json
import math
import statistics
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = json.loads((ROOT / "elastic-budget-policy.example.json").read_text())
PROFILES = {item["name"]: item for item in POLICY["profiles"]}
WEIGHTS = POLICY["learning"]["cost_weights"]
SPEC = importlib.util.spec_from_file_location(
    "elastic_budget_controller", ROOT / "elastic-budget-controller.py"
)
CONTROLLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROLLER)


def profile_args(profile):
    return [
        "-c", f'model_context_window={profile["context_window"]}',
        "-c", f'model_auto_compact_token_limit={profile["compact_token_limit"]}',
        "-c", 'model_auto_compact_token_limit_scope="body_after_prefix"',
        "-c", f'model_reasoning_effort="{profile["reasoning_effort"]}"',
        "-c", f'model_verbosity="{profile["verbosity"]}"',
        "-c", f'model_reasoning_summary="{profile["reasoning_summary"]}"',
    ]


def find_usage(value):
    if isinstance(value, dict):
        if {"input_tokens", "output_tokens"} <= value.keys():
            return value
        for child in value.values():
            found = find_usage(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_usage(child)
            if found:
                return found
    return None


def exact_score(actual, expected):
    if not isinstance(actual, dict):
        return 0.0
    return sum(actual.get(key) == value for key, value in expected.items()) / len(expected)


def token_cost_proxy(tokens):
    uncached = max(0, tokens["input"] - tokens["cached"])
    return (
        uncached * WEIGHTS["uncached_input"] + tokens["cached"] * WEIGHTS["cached_input"]
        + tokens["output"] * WEIGHTS["output"] + tokens["reasoning"] * WEIGHTS["reasoning"]
    )


def invoke(case, profile, trial_id, output_dir, timeout_seconds, model=None):
    raw_path = output_dir / "raw" / f"{trial_id}.jsonl"
    answer_path = output_dir / "answers" / f"{trial_id}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "read-only", "--output-schema", str(ROOT / "benchmarks/output.schema.json"),
        "--output-last-message", str(answer_path),
    ]
    if model:
        command.extend(["--model", model])
    command.extend([*profile_args(profile), case["prompt"]])
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command, cwd=ROOT, text=True, capture_output=True,
            stdin=subprocess.DEVNULL, timeout=timeout_seconds,
        )
        stdout, stderr, return_code = completed.stdout, completed.stderr, completed.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        stdout, stderr, return_code = error.stdout or "", error.stderr or "", 124
        if isinstance(stdout, bytes): stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes): stderr = stderr.decode(errors="replace")
    latency = time.monotonic() - started
    raw_path.write_text(stdout)
    (raw_path.with_suffix(".stderr.txt")).write_text(stderr.replace(str(Path.home()), "~"))
    events = []
    for line in stdout.splitlines():
        try: events.append(json.loads(line))
        except json.JSONDecodeError: pass
    usage = find_usage(events) or {}
    tokens = {
        "input": int(usage.get("input_tokens") or 0),
        "cached": int(usage.get("cached_input_tokens") or 0),
        "cache_write": int(usage.get("cache_write_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "reasoning": int(usage.get("reasoning_output_tokens") or 0),
    }
    answer = None
    try: answer = json.loads(answer_path.read_text()).get("answer")
    except (OSError, json.JSONDecodeError, AttributeError): pass
    return {"return_code": return_code, "timed_out": timed_out, "latency_seconds": latency,
            "tokens": tokens, "answer": answer, "model_role": model or "configured-default"}


def run_trial(case, treatment, output_dir, round_number, prices, timeout_seconds, economy_model):
    profile_name = "standard" if treatment == "baseline" else "economy"
    profile = dict(PROFILES[profile_name])
    trial_id = f'{case["id"]}-r{round_number}-{treatment}'
    stages = []
    if treatment == "baseline":
        stages.append(invoke(case, profile, trial_id, output_dir, timeout_seconds))
    else:
        contract = {"type": "json_subset", "expected": case["expected"]}
        for stage_number, stage_name in enumerate(("economy", "standard"), 1):
            stage_model = economy_model if stage_name == "economy" else None
            stage = invoke(
                case, dict(PROFILES[stage_name]), f"{trial_id}-s{stage_number}",
                output_dir, timeout_seconds, model=stage_model,
            )
            checked = CONTROLLER.verify_quality_contract({"answer": stage["answer"]}, contract)
            stage["profile"], stage["contract_passed"] = stage_name, checked["passed"]
            stages.append(stage)
            if stage["return_code"] == 0 and checked["passed"]:
                break
    final = stages[-1]
    return_code, timed_out, answer = final["return_code"], final["timed_out"], final["answer"]
    latency = sum(stage["latency_seconds"] for stage in stages)
    tokens = {key: sum(stage["tokens"][key] for stage in stages) for key in stages[0]["tokens"]}
    uncached = max(0, tokens["input"] - tokens["cached"])
    cost_proxy = token_cost_proxy(tokens)
    monetary_cost = None
    if prices:
        monetary_cost = (
            uncached * prices["uncached_input"] + tokens["cached"] * prices["cached_input"]
            + tokens["output"] * prices["output"]
        ) / 1_000_000
    return {
        "trial_id": trial_id, "case": case["id"], "round": round_number,
        "treatment": treatment, "profile": profile_name, "return_code": return_code,
        "timed_out": timed_out,
        "quality_score": exact_score(answer, case["expected"]), "answer": answer,
        "expected": case["expected"], "latency_seconds": round(latency, 3),
        "tokens": tokens, "total_tokens": tokens["input"] + tokens["output"],
        "cost_proxy": round(cost_proxy, 1), "estimated_cost": monetary_cost,
        "controller_plan": None,
        "attempt_count": len(stages),
        "stage_profiles": [stage.get("profile", profile_name) for stage in stages],
        "stage_model_roles": [stage["model_role"] for stage in stages],
        "stage_metrics": [
            {
                "profile": stage.get("profile", profile_name),
                "model_role": stage["model_role"],
                "tokens": stage["tokens"],
                "total_tokens": stage["tokens"]["input"] + stage["tokens"]["output"],
                "cost_proxy": round(token_cost_proxy(stage["tokens"]), 1),
                "latency_seconds": round(stage["latency_seconds"], 3),
                "contract_passed": stage.get("contract_passed"),
            }
            for stage in stages
        ],
        "contract_passed": treatment == "baseline" or bool(stages[-1].get("contract_passed")),
        "config": {
            "strategy": "fixed-standard" if treatment == "baseline" else "verified-progressive-inference",
            "stages": [
                {key: PROFILES[name][key] for key in (
                    "name", "context_window", "compact_token_limit", "reasoning_effort", "verbosity", "reasoning_summary"
                )}
                for name in (["standard"] if treatment == "baseline" else ["economy", "standard"])
            ],
        },
    }


def mean_sd(values):
    return {"mean": round(statistics.mean(values), 4), "sd": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0}


def metric_summary(rows, key):
    completed = [row[key] for row in rows if row["return_code"] == 0 and row.get(key) is not None]
    if not completed:
        return {"mean": None, "sd": None, "n": 0}
    return {**mean_sd(completed), "n": len(completed)}


def summarize(rows):
    result = {"groups": {}, "by_case": {}, "pairwise": {"baseline_wins": 0, "adaptive_wins": 0, "ties": 0}}
    for treatment in ("baseline", "adaptive"):
        group = [row for row in rows if row["treatment"] == treatment]
        result["groups"][treatment] = {
            "trials": len(group), "quality": mean_sd([x["quality_score"] for x in group]),
            "latency_seconds": metric_summary(group, "latency_seconds"),
            "total_tokens": metric_summary(group, "total_tokens"),
            "cost_proxy": metric_summary(group, "cost_proxy"),
            "success_rate": round(sum(x["return_code"] == 0 for x in group) / len(group), 4),
            "timeouts": sum(x["timed_out"] for x in group),
        }
    for case_name in sorted({row["case"] for row in rows}):
        result["by_case"][case_name] = {}
        for treatment in ("baseline", "adaptive"):
            group = [row for row in rows if row["case"] == case_name and row["treatment"] == treatment]
            completed = [row for row in group if row["return_code"] == 0]
            result["by_case"][case_name][treatment] = {
                "quality_mean": round(statistics.mean(x["quality_score"] for x in group), 4),
                "total_tokens_mean": round(statistics.mean(x["total_tokens"] for x in completed), 2) if completed else None,
                "cost_proxy_mean": round(statistics.mean(x["cost_proxy"] for x in completed), 2) if completed else None,
                "latency_seconds_mean": round(statistics.mean(x["latency_seconds"] for x in completed), 3) if completed else None,
                "completed": len(completed),
                "timeouts": sum(x["timed_out"] for x in group),
            }
    paired = {}
    for row in rows:
        paired.setdefault((row["case"], row["round"]), {})[row["treatment"]] = row
    for pair in paired.values():
        if set(pair) != {"baseline", "adaptive"}:
            continue
        base, adaptive = pair["baseline"], pair["adaptive"]
        base_key = (base["quality_score"], base["return_code"] == 0, -base["cost_proxy"])
        adaptive_key = (adaptive["quality_score"], adaptive["return_code"] == 0, -adaptive["cost_proxy"])
        if base_key > adaptive_key:
            result["pairwise"]["baseline_wins"] += 1
        elif adaptive_key > base_key:
            result["pairwise"]["adaptive_wins"] += 1
        else:
            result["pairwise"]["ties"] += 1
    baseline_stages = [stage for row in rows if row["treatment"] == "baseline"
                       for stage in row.get("stage_metrics", [])]
    adaptive_stages = [stage for row in rows if row["treatment"] == "adaptive"
                       for stage in row.get("stage_metrics", [])]
    baseline_standard_tokens = sum(stage["total_tokens"] for stage in baseline_stages)
    adaptive_standard_tokens = sum(
        stage["total_tokens"] for stage in adaptive_stages
        if stage["model_role"] == "configured-default"
    )
    adaptive_economy_tokens = sum(
        stage["total_tokens"] for stage in adaptive_stages
        if stage["model_role"] != "configured-default"
    )
    break_even = None
    if adaptive_economy_tokens:
        break_even = max(0.0, (baseline_standard_tokens - adaptive_standard_tokens)
                         / adaptive_economy_tokens)
    standard_calls = sum(
        stage["model_role"] == "configured-default" for stage in adaptive_stages)
    result["model_cascade"] = {
        "baseline_standard_calls": len(baseline_stages),
        "adaptive_standard_calls": standard_calls,
        "adaptive_economy_calls": sum(
            stage["model_role"] != "configured-default" for stage in adaptive_stages),
        "standard_call_reduction": round(
            1 - standard_calls / len(baseline_stages), 4) if baseline_stages else None,
        "baseline_standard_tokens": baseline_standard_tokens,
        "adaptive_standard_tokens": adaptive_standard_tokens,
        "adaptive_economy_tokens": adaptive_economy_tokens,
        "break_even_economy_to_standard_unit_price_ratio": (
            round(break_even, 4) if break_even is not None else None
        ),
    }
    return result


def write_report(output_dir, metadata, rows, summary):
    base, adaptive = summary["groups"]["baseline"], summary["groups"]["adaptive"]
    cascade = summary["model_cascade"]
    def delta(metric):
        before, after = base[metric]["mean"], adaptive[metric]["mean"]
        return None if before == 0 else round((after - before) / before * 100, 2)
    case_rows = "\n".join(
        f'| {name} | {values["baseline"]["quality_mean"]:.4f} | {values["adaptive"]["quality_mean"]:.4f} | '
        f'{values["baseline"]["total_tokens_mean"]:.1f} | {values["adaptive"]["total_tokens_mean"]:.1f} | '
        f'{values["baseline"]["timeouts"]} / {values["adaptive"]["timeouts"]} |'
        for name, values in summary["by_case"].items()
    )
    attempts = [row["attempt_count"] for row in rows if row["treatment"] == "adaptive"]
    escalations = sum(value > 1 for value in attempts)
    break_even = cascade["break_even_economy_to_standard_unit_price_ratio"]
    report = f'''# v2 真实 A/B 实验结果\n\n- 日期：{metadata["created_at"]}\n- Codex CLI：{metadata["codex_version"]}\n- baseline 标准模型：沿用本机配置，具体名称未写入仓库\n- v2 廉价阶段模型：`{metadata["economy_model"]}`；失败时回退本机标准模型\n- 轮数：每个 case 每组 {metadata["rounds"]} 轮\n- 总试验数：{len(rows)}\n- 对照：完全不使用成本控制，固定 `standard` 单次执行\n- v2：廉价模型 `economy` 首次执行；外部确定性契约不通过才升级标准模型\n- v2 升级次数：{escalations}/{len(attempts)}\n- 真实货币费用：{"已按显式单价估算" if metadata["prices_supplied"] else "未报告；provider 单价未知"}\n\n## 汇总\n\n| 指标 | 无控制 baseline | v2 验证式渐进推理 | 相对变化 |\n|---|---:|---:|---:|\n| 质量分数（均值） | {base["quality"]["mean"]:.4f} | {adaptive["quality"]["mean"]:.4f} | {delta("quality") if delta("quality") is not None else "N/A"}% |\n| 总 token（完成样本均值） | {base["total_tokens"]["mean"]:.1f} | {adaptive["total_tokens"]["mean"]:.1f} | {delta("total_tokens")}% |\n| 成本代理值（完成样本均值） | {base["cost_proxy"]["mean"]:.1f} | {adaptive["cost_proxy"]["mean"]:.1f} | {delta("cost_proxy")}% |\n| 延迟秒数（完成样本均值） | {base["latency_seconds"]["mean"]:.3f} | {adaptive["latency_seconds"]["mean"]:.3f} | {delta("latency_seconds")}% |\n| 成功率 | {base["success_rate"]:.2%} | {adaptive["success_rate"]:.2%} | — |\n| 超时次数 | {base["timeouts"]} | {adaptive["timeouts"]} | — |\n\n质量仍将超时记为 0；资源和延迟均值只使用完成样本，并在 `summary.json` 记录 `n`。本轮 token 代理和延迟都变差，不能据此宣称总体成本下降。\n\n## 模型级联结果\n\n- 标准模型调用：{cascade["baseline_standard_calls"]} → {cascade["adaptive_standard_calls"]}，减少 {cascade["standard_call_reduction"]:.0%}；\n- 廉价模型调用：{cascade["adaptive_economy_calls"]}；其中 {cascade["adaptive_economy_calls"] - escalations} 次通过契约后直接停止，{escalations} 次升级；\n- 标准模型 token：baseline {cascade["baseline_standard_tokens"]:,}，v2 升级阶段 {cascade["adaptive_standard_tokens"]:,}；廉价阶段 {cascade["adaptive_economy_tokens"]:,}；\n- 简化盈亏平衡：若廉价模型的单位 token 价格低于标准模型的 {break_even:.2%}，按本轮 token 总量才可能产生价格优势。该比例忽略不同 token 类型、缓存折扣和固定费用，只是敏感性边界，不是账单。\n\n![模型调用与 token 构成](charts/model-cascade.svg)\n\n## 按任务分解\n\n| Case | baseline 质量 | v2 质量 | baseline token | v2 token | 超时 baseline/v2 |\n|---|---:|---:|---:|---:|---:|\n{case_rows}\n\n## Pairwise\n\n- baseline 胜：{summary["pairwise"]["baseline_wins"]}\n- v2 胜：{summary["pairwise"]["adaptive_wins"]}\n- 平局：{summary["pairwise"]["ties"]}\n\n同一 case、同一轮先比较质量；质量相同时优先完成状态，再比较成本代理值。\n\n![实验总览](charts/overview.svg)\n\n![按任务对比](charts/by-case.svg)\n\n![配对差值](charts/paired-deltas.svg)\n\n![有效性边界](charts/validity-threats.svg)\n\n## 证据文件\n\n- `trials.jsonl`：逐轮结构化指标和每次升级路径；\n- `summary.json`：统计汇总；\n- `raw/`：Codex JSONL 原始事件；\n- `answers/`：各阶段结构化答案；\n- `metadata.json`：运行环境和口径。\n\n## 解释边界\n\n这是小样本、固定输入的模型级联回归实验。v2 的质量判断来自执行后外部契约，而不是模型自评；但本 benchmark 的参考答案是强 oracle，只能代表可自动验证任务。成本代理只衡量 token 类型，不含不同模型的实际单价差，因此它是保守的算力口径，不是账单。开放式写作、主观评价或契约覆盖不足的任务必须直接使用标准模型，不能据此承诺质量不下降。\n'''
    (output_dir / "REPORT.zh-CN.md").write_text(report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", default="benchmarks/results/latest")
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--economy-model", default="gpt-6-sol")
    parser.add_argument("--price-uncached-input", type=float)
    parser.add_argument("--price-cached-input", type=float)
    parser.add_argument("--price-output", type=float)
    args = parser.parse_args()
    prices = None
    supplied = [args.price_uncached_input, args.price_cached_input, args.price_output]
    if any(value is not None for value in supplied):
        if not all(value is not None for value in supplied):
            parser.error("provide all three price arguments or none")
        prices = {"uncached_input": supplied[0], "cached_input": supplied[1], "output": supplied[2]}
    output_dir = ROOT / args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    cases = json.loads((ROOT / "benchmarks/cases.json").read_text())
    version = subprocess.run(["codex", "--version"], capture_output=True, text=True).stdout.strip()
    rows = []
    for round_number in range(1, args.rounds + 1):
        for index, case in enumerate(cases):
            order = ("baseline", "adaptive") if (round_number + index) % 2 else ("adaptive", "baseline")
            for treatment in order:
                row = run_trial(
                    case, treatment, output_dir, round_number,
                    prices, args.timeout_seconds, args.economy_model,
                )
                rows.append(row)
                print(json.dumps({key: row[key] for key in ("trial_id", "return_code", "quality_score", "total_tokens", "cost_proxy", "latency_seconds")}, ensure_ascii=False), flush=True)
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(), "codex_version": version,
        "rounds": args.rounds, "cases": len(cases), "prices_supplied": bool(prices),
        "economy_model": args.economy_model,
        "method": "paired alternating-order A/B; baseline configured-default standard model; adaptive explicit economy model first with deterministic quality-contract escalation to configured-default; same prompt/schema/tools",
    }
    summary = summarize(rows)
    (output_dir / "trials.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    write_report(output_dir, metadata, rows, summary)


if __name__ == "__main__":
    main()
