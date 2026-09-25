#!/usr/bin/env python3
"""Run reproducible paired Codex CLI A/B trials and preserve raw evidence."""

import argparse
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


def profile_args(profile):
    return [
        "-c", f'model_context_window={profile["context_window"]}',
        "-c", f'model_auto_compact_token_limit={profile["compact_token_limit"]}',
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


def run_trial(case, treatment, profile_name, output_dir, round_number, prices, timeout_seconds):
    profile = PROFILES[profile_name]
    trial_id = f'{case["id"]}-r{round_number}-{treatment}'
    raw_path = output_dir / "raw" / f"{trial_id}.jsonl"
    answer_path = output_dir / "answers" / f"{trial_id}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "read-only", "--output-schema", str(ROOT / "benchmarks/output.schema.json"),
        "--output-last-message", str(answer_path), *profile_args(profile), case["prompt"],
    ]
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
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return_code = 124
    latency = time.monotonic() - started
    raw_path.write_text(stdout)
    safe_stderr = stderr.replace(str(Path.home()), "~")
    (raw_path.with_suffix(".stderr.txt")).write_text(safe_stderr)

    events = []
    for line in stdout.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    usage = find_usage(events) or {}
    tokens = {
        "input": int(usage.get("input_tokens") or 0),
        "cached": int(usage.get("cached_input_tokens") or 0),
        "cache_write": int(usage.get("cache_write_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "reasoning": int(usage.get("reasoning_output_tokens") or 0),
    }
    uncached = max(0, tokens["input"] - tokens["cached"])
    cost_proxy = (
        uncached * WEIGHTS["uncached_input"] + tokens["cached"] * WEIGHTS["cached_input"]
        + tokens["output"] * WEIGHTS["output"] + tokens["reasoning"] * WEIGHTS["reasoning"]
    )
    monetary_cost = None
    if prices:
        monetary_cost = (
            uncached * prices["uncached_input"] + tokens["cached"] * prices["cached_input"]
            + tokens["output"] * prices["output"]
        ) / 1_000_000
    answer = None
    try:
        answer = json.loads(answer_path.read_text()).get("answer")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return {
        "trial_id": trial_id, "case": case["id"], "round": round_number,
        "treatment": treatment, "profile": profile_name, "return_code": return_code,
        "timed_out": timed_out,
        "quality_score": exact_score(answer, case["expected"]), "answer": answer,
        "expected": case["expected"], "latency_seconds": round(latency, 3),
        "tokens": tokens, "total_tokens": tokens["input"] + tokens["output"],
        "cost_proxy": round(cost_proxy, 1), "estimated_cost": monetary_cost,
        "config": {key: profile[key] for key in (
            "context_window", "compact_token_limit", "reasoning_effort", "verbosity", "reasoning_summary"
        )},
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
    return result


def write_report(output_dir, metadata, rows, summary):
    base, adaptive = summary["groups"]["baseline"], summary["groups"]["adaptive"]
    def delta(metric):
        before, after = base[metric]["mean"], adaptive[metric]["mean"]
        return None if before == 0 else round((after - before) / before * 100, 2)
    case_rows = "\n".join(
        f'| {name} | {values["baseline"]["quality_mean"]:.4f} | {values["adaptive"]["quality_mean"]:.4f} | '
        f'{values["baseline"]["total_tokens_mean"]:.1f} | {values["adaptive"]["total_tokens_mean"]:.1f} | '
        f'{values["baseline"]["timeouts"]} / {values["adaptive"]["timeouts"]} |'
        for name, values in summary["by_case"].items()
    )
    report = f'''# A/B 实验结果\n\n- 日期：{metadata["created_at"]}\n- Codex CLI：{metadata["codex_version"]}\n- 模型与 provider：与本机配置保持一致，未写入仓库\n- 轮数：每个 case 每组 {metadata["rounds"]} 轮\n- 总试验数：{len(rows)}\n- 真实货币费用：{"已按显式单价估算" if metadata["prices_supplied"] else "未报告；provider 单价未知"}\n\n## 汇总\n\n| 指标 | 固定配置 baseline | 弹性 adaptive | 相对变化 |\n|---|---:|---:|---:|\n| 质量分数（均值） | {base["quality"]["mean"]:.4f} | {adaptive["quality"]["mean"]:.4f} | {delta("quality") if delta("quality") is not None else "N/A"}% |\n| 总 token（完成样本均值） | {base["total_tokens"]["mean"]:.1f} | {adaptive["total_tokens"]["mean"]:.1f} | {delta("total_tokens")}% |\n| 成本代理值（完成样本均值） | {base["cost_proxy"]["mean"]:.1f} | {adaptive["cost_proxy"]["mean"]:.1f} | {delta("cost_proxy")}% |\n| 延迟秒数（完成样本均值） | {base["latency_seconds"]["mean"]:.3f} | {adaptive["latency_seconds"]["mean"]:.3f} | {delta("latency_seconds")}% |\n| 成功率 | {base["success_rate"]:.2%} | {adaptive["success_rate"]:.2%} | — |\n| 超时次数 | {base["timeouts"]} | {adaptive["timeouts"]} | — |\n\n质量仍将超时记为 0；资源和延迟均值只使用完成样本，并在 `summary.json` 记录 `n`。超时是右删失观测，不应伪装成 0 token 或 0 成本。\n\n## 按任务分解\n\n| Case | baseline 质量 | adaptive 质量 | baseline token | adaptive token | 超时 baseline/adaptive |\n|---|---:|---:|---:|---:|---:|\n{case_rows}\n\n## Pairwise\n\n- baseline 胜：{summary["pairwise"]["baseline_wins"]}\n- adaptive 胜：{summary["pairwise"]["adaptive_wins"]}\n- 平局：{summary["pairwise"]["ties"]}\n\n同一 case、同一轮先比较质量；质量相同时优先完成状态，再比较成本代理值。\n\n![实验总览](charts/overview.svg)\n\n![按任务对比](charts/by-case.svg)\n\n![配对差值](charts/paired-deltas.svg)\n\n## 证据文件\n\n- `trials.jsonl`：逐轮结构化指标；\n- `summary.json`：统计汇总；\n- `raw/`：Codex JSONL 原始事件；\n- `answers/`：模型最终结构化答案；\n- `metadata.json`：运行环境和口径。\n\n## 解释边界\n\n这是一组小样本、同模型、固定输入的回归实验。它可以证明脚本和评测流程可复现，并展示特定任务上的质量/成本变化，但不能证明对所有真实任务普遍更优。本轮结果显示 adaptive 并未降低总体平均 token 或延迟，后续学习器应把这组结果作为负反馈，而不是宣称优化成功。\n'''
    (output_dir / "REPORT.zh-CN.md").write_text(report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", default="benchmarks/results/latest")
    parser.add_argument("--timeout-seconds", type=int, default=120)
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
                profile_name = "standard" if treatment == "baseline" else case["adaptive_profile"]
                row = run_trial(
                    case, treatment, profile_name, output_dir, round_number,
                    prices, args.timeout_seconds,
                )
                rows.append(row)
                print(json.dumps({key: row[key] for key in ("trial_id", "return_code", "quality_score", "total_tokens", "cost_proxy", "latency_seconds")}, ensure_ascii=False), flush=True)
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(), "codex_version": version,
        "rounds": args.rounds, "cases": len(cases), "prices_supplied": bool(prices),
        "method": "paired alternating-order A/B; same model/provider/prompt/schema/tools",
    }
    summary = summarize(rows)
    (output_dir / "trials.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    write_report(output_dir, metadata, rows, summary)


if __name__ == "__main__":
    main()
