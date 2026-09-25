#!/usr/bin/env python3
"""Generate dependency-free SVG charts from a benchmark trials.jsonl file."""

import argparse
import html
import json
import statistics
from pathlib import Path


COLORS = {"baseline": "#64748b", "adaptive": "#2563eb"}
TEXT = "#0f172a"
MUTED = "#64748b"
GRID = "#e2e8f0"
BG = "#ffffff"


def esc(value):
    return html.escape(str(value))


def load_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def mean(rows, key):
    values = [row[key] for row in rows if row.get(key) is not None]
    return statistics.mean(values) if values else 0.0


def svg_start(width, height, title, subtitle):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{esc(title)}</title>',
        f'<desc id="desc">{esc(subtitle)}</desc>',
        f'<rect width="{width}" height="{height}" fill="{BG}" rx="16"/>',
        f'<text x="40" y="46" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="24" font-weight="700" fill="{TEXT}">{esc(title)}</text>',
        f'<text x="40" y="72" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="13" fill="{MUTED}">{esc(subtitle)}</text>',
    ]


def text(x, y, value, size=12, color=TEXT, anchor="start", weight=400):
    return f'<text x="{x}" y="{y}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{esc(value)}</text>'


def legend(parts, x=760, y=42):
    for index, treatment in enumerate(("baseline", "adaptive")):
        offset = index * 112
        parts.append(f'<rect x="{x + offset}" y="{y - 11}" width="14" height="14" rx="3" fill="{COLORS[treatment]}"/>')
        parts.append(text(x + offset + 21, y, treatment, 12, MUTED))


def overview(rows, output):
    groups = {name: [row for row in rows if row["treatment"] == name] for name in COLORS}
    completed = {name: [row for row in group if row["return_code"] == 0] for name, group in groups.items()}
    metrics = [
        ("Quality / 质量", {name: mean(group, "quality_score") * 100 for name, group in groups.items()}, "%", 100),
        ("Success / 成功率", {name: len(completed[name]) / len(groups[name]) * 100 for name in groups}, "%", 100),
        ("Tokens (completed)", {name: mean(completed[name], "total_tokens") for name in groups}, "", None),
        ("Cost proxy (completed)", {name: mean(completed[name], "cost_proxy") for name in groups}, "", None),
        ("Latency s (completed)", {name: mean(completed[name], "latency_seconds") for name in groups}, "s", None),
    ]
    timeout_count = sum(row["timed_out"] for row in rows)
    subtitle = f"{len(rows)} trials; resource metrics exclude {timeout_count} censored timeout sample(s)"
    parts = svg_start(1280, 570, "A/B benchmark overview / 实验总览", subtitle)
    legend(parts, 1000, 46)
    left, top, chart_w, row_h = 290, 112, 830, 78
    for index, (label, values, suffix, ceiling) in enumerate(metrics):
        y = top + index * row_h
        parts.append(text(40, y + 25, label, 14, TEXT, weight=600))
        maximum = ceiling or max(values.values()) * 1.15 or 1
        for j, treatment in enumerate(("baseline", "adaptive")):
            bar_y = y + j * 27
            value = values[treatment]
            width = value / maximum * chart_w
            parts.append(f'<rect x="{left}" y="{bar_y}" width="{chart_w}" height="19" rx="5" fill="#f1f5f9"/>')
            parts.append(f'<rect x="{left}" y="{bar_y}" width="{width:.1f}" height="19" rx="5" fill="{COLORS[treatment]}"/>')
            shown = f"{value:.1f}{suffix}" if suffix or value < 1000 else f"{value:,.0f}"
            parts.append(text(left + min(width + 8, chart_w - 2), bar_y + 14, shown, 12, TEXT))
    base_timeouts = sum(row["timed_out"] for row in groups["baseline"])
    adaptive_timeouts = sum(row["timed_out"] for row in groups["adaptive"])
    parts.append(text(40, 535, f"Timeouts: baseline {base_timeouts}, adaptive {adaptive_timeouts}. Timeout records are excluded from resource means above.", 12, MUTED))
    parts.append("</svg>")
    output.write_text("\n".join(parts) + "\n")


def case_comparison(rows, output):
    cases = sorted({row["case"] for row in rows})
    metrics = [("Tokens", "total_tokens"), ("Cost proxy", "cost_proxy"), ("Latency (s)", "latency_seconds")]
    row_h = 130
    height = 150 + len(cases) * row_h
    parts = svg_start(1200, height, "Completed-run comparison by case / 按任务对比", "Means use successful runs only; n is shown per cell")
    legend(parts, 920, 46)
    panel_w = 330
    for mi, (label, key) in enumerate(metrics):
        px = 40 + mi * 355
        py = 116
        parts.append(text(px, py - 16, label, 15, TEXT, weight=700))
        case_values = {}
        max_value = 1
        for case in cases:
            case_values[case] = {}
            for treatment in COLORS:
                sample = [r for r in rows if r["case"] == case and r["treatment"] == treatment and r["return_code"] == 0]
                value = mean(sample, key)
                case_values[case][treatment] = (value, len(sample))
                max_value = max(max_value, value)
        for ci, case in enumerate(cases):
            y = py + ci * row_h
            short = case.replace("constraint_reasoning", "constraint").replace("structured_analysis", "structured")
            parts.append(text(px, y, short, 13, TEXT, weight=600))
            for tj, treatment in enumerate(("baseline", "adaptive")):
                value, n = case_values[case][treatment]
                bar_y = y + 18 + tj * 34
                width = value / max_value * (panel_w - 70)
                parts.append(f'<rect x="{px}" y="{bar_y}" width="{panel_w - 70}" height="22" rx="5" fill="#f1f5f9"/>')
                parts.append(f'<rect x="{px}" y="{bar_y}" width="{width:.1f}" height="22" rx="5" fill="{COLORS[treatment]}"/>')
                shown = f"{value:.1f}" if key == "latency_seconds" else f"{value:,.0f}"
                parts.append(text(px + min(width + 6, panel_w - 66), bar_y + 16, f"{shown} (n={n})", 11, TEXT))
            failures = [r["treatment"] for r in rows if r["case"] == case and r["return_code"] != 0]
            if failures:
                parts.append(text(px, y + 108, "timeout: " + ", ".join(failures), 11, "#b91c1c"))
    parts.append("</svg>")
    output.write_text("\n".join(parts) + "\n")


def paired_deltas(rows, output):
    pairs = {}
    for row in rows:
        pairs.setdefault((row["case"], row["round"]), {})[row["treatment"]] = row
    ordered = sorted(pairs)
    height = 170 + len(ordered) * 47
    parts = svg_start(1120, height, "Paired adaptive minus baseline / 配对差值", "Negative is better for cost proxy; timeout pairs are marked separately")
    x0, center, scale, y0 = 300, 650, 0.020, 120
    parts.append(f'<line x1="{center}" y1="100" x2="{center}" y2="{height - 55}" stroke="{TEXT}" stroke-width="1.5"/>')
    parts.append(text(center - 12, 94, "adaptive better", 11, MUTED, "end"))
    parts.append(text(center + 12, 94, "baseline better", 11, MUTED))
    deltas = []
    for index, key in enumerate(ordered):
        pair = pairs[key]
        base, adaptive = pair["baseline"], pair["adaptive"]
        y = y0 + index * 47
        label = f"{key[0]} r{key[1]}"
        parts.append(text(40, y + 5, label, 12, TEXT))
        if base["return_code"] != 0 or adaptive["return_code"] != 0:
            loser = "baseline" if base["return_code"] != 0 else "adaptive"
            color = COLORS["adaptive"] if loser == "baseline" else COLORS["baseline"]
            x = center - 250 if loser == "baseline" else center + 250
            parts.append(f'<circle cx="{x}" cy="{y}" r="8" fill="{color}"/>')
            parts.append(text(x + (-14 if loser == "baseline" else 14), y + 4, f"{loser} timeout", 11, "#b91c1c", "end" if loser == "baseline" else "start"))
            continue
        delta = adaptive["cost_proxy"] - base["cost_proxy"]
        deltas.append(delta)
        x = max(x0, min(1000, center + delta * scale))
        color = COLORS["adaptive"] if delta < 0 else COLORS["baseline"]
        parts.append(f'<line x1="{center}" y1="{y}" x2="{x}" y2="{y}" stroke="{color}" stroke-width="5" stroke-linecap="round"/>')
        parts.append(f'<circle cx="{x}" cy="{y}" r="7" fill="{color}"/>')
        parts.append(text(x + (-10 if delta < 0 else 10), y - 8, f"{delta:+,.0f}", 11, TEXT, "end" if delta < 0 else "start"))
    if deltas:
        parts.append(text(40, height - 25, f"Completed pairs median cost-proxy delta: {statistics.median(deltas):+,.0f}; mean: {statistics.mean(deltas):+,.0f}", 12, MUTED))
    parts.append("</svg>")
    output.write_text("\n".join(parts) + "\n")


def threats_chart(rows, output):
    cases = len({row["case"] for row in rows})
    rounds = len({row["round"] for row in rows})
    items = [
        ("Profile bundle", "Window, threshold, reasoning, verbosity and summary change together", "HIGH"),
        ("Small sample", f"{cases} cases × {rounds} round(s); uncertainty is dominant", "HIGH"),
        ("Cold start", "The online learner has no production feedback in this isolated run", "HIGH"),
        ("Short contexts", "These prompts do not stress long-context selection or compaction", "HIGH"),
        ("Cache/order effects", "Alternating order reduces but cannot eliminate temporal cache effects", "MED"),
        ("Proxy cost", "Weighted tokens are not a provider invoice without explicit prices", "MED"),
    ]
    parts = svg_start(1120, 560, "Validity threats / 有效性问题", "Why this run is evidence of a harness, not proof of an optimization win")
    for index, (name, detail, level) in enumerate(items):
        y = 105 + index * 70
        color = "#dc2626" if level == "HIGH" else "#d97706"
        parts.append(f'<rect x="40" y="{y - 23}" width="1040" height="54" rx="10" fill="#f8fafc" stroke="{GRID}"/>')
        parts.append(f'<rect x="58" y="{y - 9}" width="54" height="24" rx="12" fill="{color}"/>')
        parts.append(text(85, y + 7, level, 11, "#ffffff", "middle", 700))
        parts.append(text(132, y, name, 14, TEXT, weight=700))
        parts.append(text(132, y + 20, detail, 12, MUTED))
    parts.append("</svg>")
    output.write_text("\n".join(parts) + "\n")


def model_cascade(rows, output):
    baseline = [stage for row in rows if row["treatment"] == "baseline" for stage in row.get("stage_metrics", [])]
    adaptive = [stage for row in rows if row["treatment"] == "adaptive" for stage in row.get("stage_metrics", [])]
    base_standard = sum(stage["total_tokens"] for stage in baseline)
    adaptive_standard = sum(stage["total_tokens"] for stage in adaptive if stage["model_role"] == "configured-default")
    adaptive_economy = sum(stage["total_tokens"] for stage in adaptive if stage["model_role"] != "configured-default")
    base_calls = len(baseline)
    standard_calls = sum(stage["model_role"] == "configured-default" for stage in adaptive)
    economy_calls = len(adaptive) - standard_calls
    ratio = (base_standard - adaptive_standard) / adaptive_economy if adaptive_economy else 0
    parts = svg_start(1120, 480, "Verifier-gated model cascade / 验证式模型级联", "Equal contract quality; monetary savings depend on relative model prices")
    parts.append(text(55, 120, "Model calls / 模型调用", 16, TEXT, weight=700))
    max_calls = max(base_calls, standard_calls + economy_calls, 1)
    for index, (label, standard, economy) in enumerate((("baseline", base_calls, 0), ("v2 adaptive", standard_calls, economy_calls))):
        y = 155 + index * 62
        scale = 470 / max_calls
        parts.append(text(55, y + 20, label, 13, TEXT))
        parts.append(f'<rect x="180" y="{y}" width="{standard * scale:.1f}" height="28" rx="5" fill="#64748b"/>')
        parts.append(f'<rect x="{180 + standard * scale:.1f}" y="{y}" width="{economy * scale:.1f}" height="28" rx="5" fill="#2563eb"/>')
        parts.append(text(670, y + 20, f"standard {standard}, economy {economy}", 12, MUTED))
    parts.append(text(55, 315, "Stage token composition / 分阶段 token", 16, TEXT, weight=700))
    maximum = max(base_standard, adaptive_standard + adaptive_economy, 1)
    for index, (label, standard, economy) in enumerate((("baseline", base_standard, 0), ("v2 adaptive", adaptive_standard, adaptive_economy))):
        y = 345 + index * 48
        scale = 600 / maximum
        parts.append(text(55, y + 17, label, 13, TEXT))
        parts.append(f'<rect x="180" y="{y}" width="{standard * scale:.1f}" height="24" rx="4" fill="#64748b"/>')
        parts.append(f'<rect x="{180 + standard * scale:.1f}" y="{y}" width="{economy * scale:.1f}" height="24" rx="4" fill="#2563eb"/>')
        parts.append(text(800, y + 17, f"standard {standard:,}; economy {economy:,}", 12, MUTED))
    parts.append(text(55, 455, f"Break-even economy/standard unit-token price ratio: {ratio:.2%} (simplified sensitivity, not an invoice)", 12, MUTED))
    parts.append("</svg>")
    output.write_text("\n".join(parts) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="?", default="benchmarks/results/2026-09-25")
    args = parser.parse_args()
    result_dir = Path(args.results)
    rows = load_rows(result_dir / "trials.jsonl")
    chart_dir = result_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    overview(rows, chart_dir / "overview.svg")
    case_comparison(rows, chart_dir / "by-case.svg")
    paired_deltas(rows, chart_dir / "paired-deltas.svg")
    threats_chart(rows, chart_dir / "validity-threats.svg")
    model_cascade(rows, chart_dir / "model-cascade.svg")
    print(f"wrote 5 charts to {chart_dir}")


if __name__ == "__main__":
    main()
