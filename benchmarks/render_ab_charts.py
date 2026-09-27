#!/usr/bin/env python3
"""Render lightweight, dependency-free SVG charts for a completed A/B run."""
import json
import sys
from pathlib import Path

def bar_chart(path, title, labels, left, right, left_name="baseline", right_name="adaptive"):
    width, height = 900, 420
    maximum = max(left + right) or 1
    rows = []
    for i, label in enumerate(labels):
        y = 82 + i * 58
        a, b = 300 * left[i] / maximum, 300 * right[i] / maximum
        rows += [f'<text x="20" y="{y+18}" font-size="14">{label}</text>',
                 f'<rect x="190" y="{y}" width="{a:.1f}" height="18" fill="#4263eb"/>',
                 f'<rect x="500" y="{y}" width="{b:.1f}" height="18" fill="#12b886"/>',
                 f'<text x="495" y="{y+14}" text-anchor="end" font-size="12">{left[i]:,.0f}</text>',
                 f'<text x="805" y="{y+14}" font-size="12">{right[i]:,.0f}</text>']
    path.write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
<rect width="100%" height="100%" fill="white"/><text x="20" y="30" font-size="20" font-weight="bold">{title}</text>
<text x="190" y="60" font-size="13" fill="#4263eb">{left_name}</text><text x="500" y="60" font-size="13" fill="#12b886">{right_name}</text>
{''.join(rows)}</svg>''')

def main():
    if len(sys.argv) != 2: raise SystemExit("usage: render_ab_charts.py RESULT_DIR")
    root = Path(sys.argv[1]); summary = json.loads((root / "summary.json").read_text())
    out = root / "charts"; out.mkdir(exist_ok=True)
    names = sorted(summary["by_case"])
    pick = lambda key, treatment: [summary["by_case"][n][treatment][key] for n in names]
    bar_chart(out / "by-case-tokens.svg", "Mean total tokens by case", names, pick("total_tokens_mean", "baseline"), pick("total_tokens_mean", "adaptive"))
    bar_chart(out / "by-case-cost-proxy.svg", "Mean weighted token cost proxy by case", names, pick("cost_proxy_mean", "baseline"), pick("cost_proxy_mean", "adaptive"))
    c = summary["model_cascade"]
    bar_chart(out / "model-cascade.svg", "Calls and token composition", ["standard calls", "standard tokens", "economy tokens"], [c["baseline_standard_calls"], c["baseline_standard_tokens"], 0], [c["adaptive_standard_calls"], c["adaptive_standard_tokens"], c["adaptive_economy_tokens"]])

if __name__ == "__main__": main()
