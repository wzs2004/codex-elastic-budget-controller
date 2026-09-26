#!/usr/bin/env python3
"""Reproducible A/B benchmark for the extractive output compactor."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("controller", ROOT / "elastic-budget-controller.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixtures():
    cases = []
    cases.append(("pytest", "\n".join(["============================= test session starts =============================", *(["PASSED test_widget.py::test_render"] * 80), "FAILED test_api.py::test_timeout - AssertionError: expected 200, got 504", "=========================== short test summary info ============================"])))
    cases.append(("build", "\n".join(["npm run build", *(["Bundling module..."] * 35), "warning: asset size exceeds recommended limit", "dist/app.js 1.2 MiB", "Build complete"])))
    cases.append(("logs", "\n".join(["2026-09-26T01:00:00Z INFO worker heartbeat"] * 120 + ["2026-09-26T01:01:03Z ERROR database connection refused", "Traceback: retry budget exhausted"])))
    cases.append(("git", "\n".join([" M src/file.py", " M README.md"] * 45 + ["fatal: pre-commit hook failed"])))
    cases.append(("json", json.dumps({"status": "ok", "items": [{"id": i, "value": i * 3} for i in range(100)]}, indent=2)))
    cases.append(("mixed", "\n".join(["", "\u001b[32mPASS\u001b[0m", "\u001b[32mPASS\u001b[0m"] * 55 + ["WARNING: cache miss", "ERROR: permission denied: /srv/app", "exit code 1"])))
    return cases


def main():
    rows = []
    for name, raw in fixtures():
        compact = MODULE.shrink_output(raw, max_lines=160, max_chars=16000)
        original_tokens = MODULE.estimate_request_tokens(raw)
        compact_tokens = MODULE.estimate_request_tokens(compact["text"])
        important = [line for line in raw.splitlines() if MODULE.IMPORTANT_OUTPUT.search(line)]
        retained = sum(line in compact["text"] for line in important)
        rows.append({
            "case": name, "original_chars": len(raw),
            "compressed_chars": len(compact["text"]),
            "original_tokens_estimate": original_tokens,
            "compressed_tokens_estimate": compact_tokens,
            "token_reduction_percent": round(100 * (1 - compact_tokens / max(1, original_tokens)), 2),
            "important_lines": len(important), "important_lines_retained": retained,
            "archive_not_used": compact["archive_path"] is None,
        })
    total_original = sum(row["original_tokens_estimate"] for row in rows)
    total_compact = sum(row["compressed_tokens_estimate"] for row in rows)
    result = {
        "kind": "extractive_output_compaction_mechanism_benchmark",
        "cases": len(rows), "token_estimator": "controller.estimate_request_tokens",
        "total_original_tokens_estimate": total_original,
        "total_compressed_tokens_estimate": total_compact,
        "token_reduction_percent": round(100 * (1 - total_compact / max(1, total_original)), 2),
        "important_line_retention": round(sum(r["important_lines_retained"] for r in rows) / max(1, sum(r["important_lines"] for r in rows)), 4),
        "rows": rows,
        "caveat": "Mechanism benchmark only; estimator is not provider billing and no model quality claim is made.",
    }
    output = ROOT / "benchmarks/results/2026-09-26-output-compaction.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
