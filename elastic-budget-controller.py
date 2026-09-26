#!/usr/bin/python3
"""Adaptive per-request and long-context budget controller.

Only standard-library modules are used so the LaunchAgent can run it with the
macOS system Python. Decisions are discrete, rate-limited, logged, and atomic.
"""

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CONFIG = ROOT / "config.toml"
POLICY = ROOT / "elastic-budget-policy.json"
STATE = ROOT / "elastic-budget-state.json"
DECISIONS = ROOT / "elastic-budget-decisions.jsonl"
SESSIONS = ROOT / "sessions"

FORGETTING_PATTERNS = (
    "又确认", "重复确认", "忘了", "上下文压缩太", "再确认一遍",
    "forgot", "forgetting", "asked again", "repeat confirmation",
    "lost context", "context compression",
)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
IMPORTANT_OUTPUT = re.compile(
    r"(?i)(error|failed|failure|exception|traceback|warning|warn|panic|"
    r"security|permission|denied|assert|todo|fixme|❌|⚠|✗|✘)"
)


def shrink_output(
    text: str, max_lines: int = 160, max_chars: int = 16000,
    archive_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compress noisy tool output without deleting actionable evidence.

    This is intentionally deterministic and extractive: ANSI noise and blank
    runs are removed, consecutive duplicates are collapsed, and only when the
    hard budget is still exceeded are middle lines omitted. The original can
    optionally be archived for exact recovery.
    """
    original = str(text or "")
    cleaned = ANSI_ESCAPE.sub("", original).replace("\r", "")
    source = cleaned.splitlines()
    lines: List[str] = []
    blank_run = 0
    index = 0
    while index < len(source):
        line = source[index].rstrip()
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                lines.append("")
            index += 1
            continue
        blank_run = 0
        repeat = 1
        while index + repeat < len(source) and source[index + repeat].rstrip() == line:
            repeat += 1
        lines.append(line)
        if repeat > 2:
            lines.append(f"… repeated {repeat - 1} times")
        index += repeat
    omitted = 0
    max_lines = max(12, int(max_lines))
    if len(lines) > max_lines:
        head = max_lines // 3
        tail = max_lines // 3
        important = [
            line for line in lines[head:-tail]
            if IMPORTANT_OUTPUT.search(line)
        ]
        middle_budget = max_lines - head - tail - 1
        kept_important = important[:max(0, middle_budget)]
        omitted = len(lines) - head - tail - len(kept_important)
        lines = lines[:head] + kept_important + [
            f"… {omitted} lines omitted; recover the archived output if needed"
        ] + lines[-tail:]
    result = "\n".join(lines)
    if len(result) > max_chars:
        marker = "\n… output truncated; recover the archived output if needed\n"
        keep = max(0, int(max_chars) - len(marker))
        result = result[:keep] + marker
    archive_path = None
    if archive_dir is not None:
        archive_dir = Path(archive_dir)
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / f"output-{uuid.uuid4().hex}.log"
        atomic_write(archive_path, original)
    return {
        "text": result,
        "original_chars": len(original),
        "compressed_chars": len(result),
        "original_lines": len(source),
        "compressed_lines": len(result.splitlines()),
        "compression_ratio": round(len(result) / max(1, len(original)), 4),
        "omitted_lines": omitted,
        "archive_path": str(archive_path) if archive_path else None,
    }

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def atomic_write(path: Path, text: str, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is None and path.exists():
            mode = path.stat().st_mode & 0o777
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def setting(text: str, key: str) -> Optional[str]:
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.+?)\s*$", text)
    return match.group(1) if match else None


def replace_setting(text: str, key: str, value: str) -> str:
    pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
    replacement = f"{key} = {value}"
    if re.search(pattern, text):
        return re.sub(pattern, replacement, text, count=1)
    return replacement + "\n" + text


def desired_config(
    text: str, profile: Dict[str, Any], compact_token_limit: Optional[int] = None,
) -> str:
    updated = text
    updated = replace_setting(
        updated, "model_reasoning_effort", json.dumps(profile["reasoning_effort"])
    )
    updated = replace_setting(
        updated, "model_context_window", str(profile["context_window"])
    )
    updated = replace_setting(
        updated, "model_auto_compact_token_limit",
        str(compact_token_limit or profile["compact_token_limit"])
    )
    updated = replace_setting(
        updated, "model_auto_compact_token_limit_scope", '"body_after_prefix"'
    )
    updated = replace_setting(
        updated, "model_verbosity", json.dumps(profile.get("verbosity", "low"))
    )
    updated = replace_setting(
        updated, "model_reasoning_summary",
        json.dumps(profile.get("reasoning_summary", "concise"))
    )
    return updated


def elastic_compact_token_limit(
    profile: Dict[str, Any], metrics: Dict[str, Any], policy: Dict[str, Any],
) -> Tuple[int, float]:
    """Continuously adjust the in-profile compaction threshold.

    Higher live pressure receives more headroom, while calm sessions compact
    earlier. The result is clamped and quantized to avoid config churn.
    """
    elastic = policy.get("elastic_compaction", {})
    minimum = float(profile.get("compact_min_ratio", elastic.get("min_ratio", 0.58)))
    maximum = float(profile.get("compact_max_ratio", elastic.get("max_ratio", 0.94)))
    base = float(profile.get("compact_base_ratio", elastic.get("base_ratio", 0.74)))
    quantum = max(1, int(elastic.get("quantum_tokens", 1024)))

    occupancy = max(0.0, min(1.0, float(metrics.get("occupancy") or 0.0)))
    growth_limit = max(1.0, float(policy["signals"]["growth_high_tokens_per_minute"]))
    growth = min(1.0, float(metrics.get("growth_tokens_per_minute") or 0.0) / growth_limit)
    tool_limit = max(1.0, float(policy["signals"]["tool_calls_high"]))
    tools = min(1.0, float(metrics.get("tool_calls") or 0.0) / tool_limit)
    turn_limit = max(1.0, float(policy["signals"]["turns_high"]))
    turns = min(1.0, float(metrics.get("turns") or 0.0) / turn_limit)
    compactions = int(metrics.get("compactions") or 0)

    pressure = 0.45 * occupancy + 0.25 * growth + 0.20 * tools + 0.10 * turns
    ratio = base + (maximum - base) * pressure
    if compactions:
        ratio = max(ratio, maximum - 0.01)
    if compactions >= int(policy.get("repeat_compaction_count", 2)):
        ratio = maximum
    ratio = max(minimum, min(maximum, ratio))

    window = int(profile["context_window"])
    tokens = int(round((window * ratio) / quantum) * quantum)
    reserve = int(elastic.get("minimum_reserve_tokens", 16384))
    tokens = max(quantum, min(window - reserve, tokens))
    return tokens, round(tokens / window, 4)


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def estimate_request_tokens(text: str) -> int:
    """Return a conservative tokenizer-free estimate for preflight routing."""
    if not text:
        return 1
    ascii_count = sum(1 for character in text if ord(character) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, math.ceil(ascii_count / 4.0 + non_ascii_count / 1.5))


def normalize_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize cheap, model-independent features available before a request."""
    text = str(request.get("text") or request.get("prompt") or "")
    estimated = int(request.get("estimated_input_tokens") or estimate_request_tokens(text))
    task_type = str(request.get("task_type") or "general").lower()
    complexity_defaults = {
        "classification": 0.15, "extraction": 0.20, "rewrite": 0.20,
        "general": 0.40, "analysis": 0.60, "coding": 0.70,
        "research": 0.80, "high_stakes": 0.95,
    }
    complexity = clamp(float(request.get("complexity", complexity_defaults.get(task_type, 0.40))))
    quality_risk = clamp(float(request.get("quality_risk", complexity)))
    expected_tool_calls = max(0, int(request.get("expected_tool_calls") or 0))
    expected_turns = max(1, int(request.get("expected_turns") or 1))
    return {
        "estimated_input_tokens": estimated,
        "task_type": task_type,
        "complexity": round(complexity, 4),
        "quality_risk": round(quality_risk, 4),
        "expected_tool_calls": expected_tool_calls,
        "expected_turns": expected_turns,
        "latency_sensitive": bool(request.get("latency_sensitive", False)),
        "reusable_prefix": bool(request.get("reusable_prefix", expected_turns > 1)),
    }


def lexical_terms(text: str) -> set:
    """Return cheap multilingual retrieval terms without dependencies."""
    lowered = text.lower()
    terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    terms.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    return {term for term in terms if term}


def select_context_segments(
    request: Dict[str, Any], target_tokens: int, policy: Dict[str, Any],
) -> Dict[str, Any]:
    """Select query-relevant segments while preserving protected boundaries."""
    raw_segments = request.get("context_segments") or []
    if not raw_segments:
        return {"applied": False, "reason": "no context_segments supplied",
                "input_tokens": 0, "selected_tokens": 0, "selected_indices": []}
    segments = []
    for index, raw in enumerate(raw_segments):
        item = dict(raw) if isinstance(raw, dict) else {"text": str(raw)}
        content = str(item.get("text") or "")
        segments.append({"index": index, "text": content,
                         "tokens": int(item.get("tokens") or estimate_request_tokens(content)),
                         "must_keep": bool(item.get("must_keep", False)),
                         "stable": bool(item.get("stable", False)),
                         "role": str(item.get("role") or "context")})
    total = sum(item["tokens"] for item in segments)
    budget = max(1, min(int(target_tokens), total))
    query = " ".join(str(request.get(key) or "") for key in ("text", "prompt", "query"))
    query_terms = lexical_terms(query)
    last_index = len(segments) - 1
    preserve = bool(policy.get("context_selection", {}).get("preserve_boundaries", True))
    protected = {item["index"] for item in segments if item["must_keep"]
                 or item["role"] in {"system", "developer", "instruction", "schema"}
                 or (preserve and item["index"] in {0, last_index})}

    def relevance(item: Dict[str, Any]) -> Tuple[float, int]:
        overlap = len(query_terms & lexical_terms(item["text"])) / max(1, len(query_terms))
        recency = item["index"] / max(1, last_index)
        structure = 1.0 if re.search(r"(?m)^(#{1,6}\s|[A-Z][A-Z ]+:|第.{1,8}[章节])", item["text"]) else 0.0
        return 0.62 * overlap + 0.18 * recency + 0.12 * structure + 0.08 * float(item["stable"]), -item["index"]

    selected = set(protected)
    used = sum(segments[index]["tokens"] for index in selected)
    for item in sorted((item for item in segments if item["index"] not in selected),
                       key=relevance, reverse=True):
        if used + item["tokens"] <= budget or not selected:
            selected.add(item["index"]); used += item["tokens"]
    ordered = sorted(selected)
    return {"applied": used < total, "method": "deterministic-query-aware-extractive",
            "input_tokens": total, "target_tokens": budget, "selected_tokens": used,
            "compression_ratio": round(used / max(1, total), 4),
            "selected_indices": ordered,
            "dropped_indices": [item["index"] for item in segments if item["index"] not in selected],
            "selected_segments": [segments[index]["text"] for index in ordered],
            "protected_indices": sorted(protected)}


def prompt_layout_plan(request: Dict[str, Any], selection: Dict[str, Any]) -> Dict[str, Any]:
    """Describe a prompt-cache-friendly stable-prefix layout."""
    segments = request.get("context_segments") or []
    selected = set(selection.get("selected_indices") or range(len(segments)))
    stable, dynamic = [], []
    for index, raw in enumerate(segments):
        if index not in selected:
            continue
        item = dict(raw) if isinstance(raw, dict) else {"text": str(raw)}
        destination = stable if item.get("stable") or item.get("role") in {
            "system", "developer", "instruction", "schema"} else dynamic
        destination.append(index)
    return {"stable_prefix_indices": stable, "dynamic_suffix_indices": dynamic,
            "append_only_history": True,
            "cache_breakpoint_after_segment": stable[-1] if stable else None}


def arm_guardrail_status(item: Dict[str, Any], learner: Dict[str, Any]) -> Tuple[bool, str]:
    """Apply confidence-aware quality, failure and latency constraints."""
    attempts = int(item.get("count") or 0)
    minimum = int(learner.get("guardrail_minimum_trials", learner.get("minimum_trials", 2)))
    if attempts < minimum:
        return True, "insufficient evidence for exclusion"
    failures = int(item.get("failures") or 0)
    z = float(learner.get("confidence_z", 1.64))
    failure_rate = failures / max(1, attempts)
    denominator = 1.0 + z * z / attempts
    failure_upper = (
        failure_rate + z * z / (2 * attempts)
        + z * math.sqrt((failure_rate * (1 - failure_rate) + z * z / (4 * attempts)) / attempts)
    ) / denominator
    if failure_upper > float(learner.get("max_failure_rate_upper", 0.35)):
        return False, "failure-rate guardrail"
    quality_mean = float(item.get("quality_mean") or 0.0)
    variance = float(item.get("quality_m2") or 0.0) / max(1, attempts - 1)
    quality_lower = quality_mean - z * math.sqrt(max(0.0, variance) / attempts)
    if quality_lower < float(learner.get("minimum_quality", 0.65)):
        return False, "quality-floor guardrail"
    if float(item.get("latency_seconds_total") or 0.0) / max(1, attempts) > float(learner.get("max_mean_latency_seconds", 90)):
        return False, "latency guardrail"
    return True, "within guardrails"


def identity_matrix(size: int) -> List[List[float]]:
    return [[1.0 if row == column else 0.0 for column in range(size)] for row in range(size)]


def inverse_matrix(matrix: List[List[float]]) -> List[List[float]]:
    """Invert a small positive-definite matrix with pivoted Gauss-Jordan."""
    size = len(matrix)
    augmented = [list(map(float, row)) + identity_matrix(size)[index]
                 for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-10:
            return identity_matrix(size)
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                augmented[row][index] - factor * augmented[column][index]
                for index in range(2 * size)
            ]
    return [row[size:] for row in augmented]


def arm_matrix(item: Dict[str, Any], size: int) -> List[List[float]]:
    matrix = item.get("a_matrix")
    if isinstance(matrix, list) and len(matrix) == size and all(
        isinstance(row, list) and len(row) == size for row in matrix
    ):
        return [[float(value) for value in row] for row in matrix]
    diagonal = list(item.get("a_diagonal") or [1.0] * size)
    if len(diagonal) != size:
        diagonal = [1.0] * size
    return [[float(diagonal[row]) if row == column else 0.0
             for column in range(size)] for row in range(size)]


def choose_request_profile(
    tier: str, base_profile: str, request: Dict[str, Any], policy: Dict[str, Any],
    learning: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, float]:
    """Choose a safe profile arm with deterministic conservative UCB."""
    settings = policy.get("request_budgeting", {})
    candidates = list((settings.get("candidate_profiles") or {}).get(tier) or [base_profile])
    profiles = {item["name"]: item for item in policy["profiles"]}
    candidates = [name for name in candidates if name in profiles]
    if not candidates:
        return base_profile, "configured base profile", 1.0
    learner = settings.get("learning", {})
    stats = (learning or {}).get("arms") or {}
    minimum_trials = int(learner.get("minimum_trials", 2))
    eligible = []
    for name in candidates:
        item = stats.get(f"{tier}:{name}") or {}
        safe, _ = arm_guardrail_status(item, learner)
        if safe:
            eligible.append(name)
    eligible = eligible or ([base_profile] if base_profile in candidates else candidates[:1])
    under_tested = [
        name for name in eligible
        if int((stats.get(f"{tier}:{name}") or {}).get("count") or 0) < minimum_trials
    ]
    request_key = json.dumps(request, ensure_ascii=False, sort_keys=True)
    seed = int(hashlib.sha256(request_key.encode()).hexdigest()[:8], 16)
    if under_tested:
        levels = {name: int(profiles[name].get("capacity_level", 0)) for name in under_tested}
        selected = min(
            under_tested,
            key=lambda name: (int((stats.get(f"{tier}:{name}") or {}).get("count") or 0),
                              levels[name], name),
        )
        return (
            selected,
            "cost-ordered safe minimum-trial exploration",
            1.0,
        )
    exploration = float(learner.get("ucb_exploration", 0.35))
    features = request_feature_vector(normalize_request(request))

    def value(name: str) -> float:
        item = stats.get(f"{tier}:{name}") or {}
        matrix = arm_matrix(item, len(features))
        response = list(item.get("b_vector") or [0.0] * len(features))
        if len(response) != len(features):
            response = [0.0] * len(features)
        inverse = inverse_matrix(matrix)
        theta = [sum(inverse[row][column] * response[column]
                     for column in range(len(features))) for row in range(len(features))]
        mean = sum(theta[index] * feature for index, feature in enumerate(features))
        projected = [sum(inverse[row][column] * features[column]
                         for column in range(len(features))) for row in range(len(features))]
        uncertainty = math.sqrt(max(0.0, sum(features[index] * projected[index]
                                             for index in range(len(features)))))
        capacity = int(profiles[name].get("capacity_level", 0))
        cost_prior = float(learner.get("capacity_cost_prior", 0.025)) * capacity
        cost_prior *= 1.0 - float(features[2])
        return mean + exploration * uncertainty - cost_prior

    greedy = max(eligible, key=value)
    total_trials = sum(int((stats.get(f"{tier}:{name}") or {}).get("count") or 0)
                       for name in eligible)
    epsilon = clamp(float(learner.get("epsilon", 0.08)), 0.0, 0.5)
    epsilon /= math.sqrt(1.0 + total_trials / max(
        1.0, float(learner.get("exploration_decay_trials", 20))))
    epsilon *= 1.0 - float(learner.get("high_risk_exploration_suppression", 0.75)) * features[2]
    epsilon = clamp(epsilon, float(learner.get("minimum_epsilon", 0.01)), 0.5)
    unit = seed / float(0xFFFFFFFF)
    if len(eligible) > 1 and unit < epsilon:
        selected = eligible[seed % len(eligible)]
        reason = "risk-adjusted safe exploration around full LinUCB"
    else:
        selected = greedy
        reason = "confidence-guarded full LinUCB selection"
    propensity = epsilon / len(eligible)
    if selected == greedy:
        propensity += 1.0 - epsilon
    return selected, reason, round(propensity, 6)


def request_feature_vector(features: Dict[str, Any]) -> List[float]:
    """Dense, bounded features for online per-request learning."""
    return [
        1.0,
        float(features["complexity"]),
        float(features["quality_risk"]),
        clamp(math.log1p(float(features["estimated_input_tokens"])) / math.log(131073)),
        clamp(float(features["expected_tool_calls"]) / 12.0),
        clamp((float(features["expected_turns"]) - 1.0) / 9.0),
        float(bool(features["latency_sensitive"])),
        float(bool(features["reusable_prefix"])),
    ]


def update_request_learning(
    state: Dict[str, Any], feedback: Dict[str, Any], policy: Dict[str, Any],
) -> Dict[str, Any]:
    """Update a request-level arm from observed quality, cost, latency and status."""
    tier = str(feedback.get("tier") or "")
    profile = str(feedback.get("profile") or "")
    if not tier or not profile:
        raise ValueError("feedback requires tier and profile")
    learner = policy.get("request_budgeting", {}).get("learning", {})
    quality = clamp(float(feedback.get("quality_score", 0.0)))
    success = bool(feedback.get("success", True))
    cost = max(0.0, float(feedback.get("cost_tokens") or 0.0))
    latency = max(0.0, float(feedback.get("latency_seconds") or 0.0))
    reward = (
        quality
        - cost / max(1.0, float(learner.get("cost_scale_tokens", 50000)))
        - latency / max(1.0, float(learner.get("latency_scale_seconds", 120)))
        - (0.0 if success else float(learner.get("failure_penalty", 1.0)))
    )
    feedback["reward"] = round(reward, 8)
    request_learning = dict(state.get("request_learning") or {})
    arms = dict(request_learning.get("arms") or {})
    decay = clamp(float(learner.get("geometric_decay", 0.98)), 0.5, 1.0)
    raw_features = feedback.get("features") or {
        "estimated_input_tokens": feedback.get("estimated_input_tokens", 1),
        "task_type": feedback.get("task_type", "general"),
        "complexity": feedback.get("complexity", 0.4),
        "quality_risk": feedback.get("quality_risk", 0.4),
        "expected_tool_calls": feedback.get("expected_tool_calls", 0),
        "expected_turns": feedback.get("expected_turns", 1),
        "latency_sensitive": feedback.get("latency_sensitive", False),
        "reusable_prefix": feedback.get("reusable_prefix", False),
    }
    features = request_feature_vector(normalize_request(raw_features))
    for arm_key, arm_value in list(arms.items()):
        arm = dict(arm_value)
        if "a_matrix" in arm or "a_diagonal" in arm:
            matrix = arm_matrix(arm, len(features))
            arm["a_matrix"] = [
                [round((1.0 if row == column else 0.0)
                       + (matrix[row][column] - (1.0 if row == column else 0.0)) * decay, 8)
                 for column in range(len(features))]
                for row in range(len(features))
            ]
            arm["b_vector"] = [float(value) * decay for value in arm.get("b_vector", [])]
            arm.pop("a_diagonal", None)
            arms[arm_key] = arm
    key = f"{tier}:{profile}"
    item = dict(arms.get(key) or {})
    count = int(item.get("count") or 0) + 1
    total_reward = float(item.get("total_reward") or 0.0) + reward
    old_quality_mean = float(item.get("quality_mean") or 0.0)
    quality_delta = quality - old_quality_mean
    new_quality_mean = old_quality_mean + quality_delta / count
    quality_m2 = float(item.get("quality_m2") or 0.0) + quality_delta * (quality - new_quality_mean)
    item.update({
        "count": count,
        "total_reward": round(total_reward, 6),
        "mean_reward": round(total_reward / count, 6),
        "failures": int(item.get("failures") or 0) + int(not success),
        "quality_mean": round(new_quality_mean, 6),
        "quality_m2": round(quality_m2, 8),
        "cost_tokens_total": round(float(item.get("cost_tokens_total") or 0.0) + cost, 2),
        "latency_seconds_total": round(float(item.get("latency_seconds_total") or 0.0) + latency, 3),
        "last_feedback_at": iso(utcnow()),
    })
    matrix = arm_matrix(item, len(features))
    response = list(item.get("b_vector") or [0.0] * len(features))
    item["a_matrix"] = [
        [round(matrix[row][column] + features[row] * features[column], 8)
         for column in range(len(features))]
        for row in range(len(features))
    ]
    item.pop("a_diagonal", None)
    item["b_vector"] = [round(response[index] + reward * value, 8)
                        for index, value in enumerate(features)]
    arms[key] = item
    drift_settings = learner.get("drift_detection", {})
    drifts = dict(request_learning.get("drifts") or {})
    drift = dict(drifts.get(tier) or {})
    drift_count = int(drift.get("count") or 0) + 1
    drift_mean = float(drift.get("mean_reward") or reward)
    drift_mean += (reward - drift_mean) / drift_count
    cumulative = float(drift.get("cumulative_sum") or 0.0)
    cumulative += reward - drift_mean + float(drift_settings.get("delta", 0.01))
    maximum = max(float(drift.get("maximum_sum") or 0.0), cumulative)
    cumulative_drop = maximum - cumulative
    threshold = float(drift_settings.get("threshold", 1.25))
    detected = bool(drift_settings.get("enabled", True) and drift_count >= int(
        drift_settings.get("minimum_observations", 20)) and cumulative_drop > threshold)
    if detected:
        shrink = clamp(float(drift_settings.get("matrix_retention", 0.25)), 0.0, 1.0)
        tier_prefix = f"{tier}:"
        for arm_key, arm_value in list(arms.items()):
            if not arm_key.startswith(tier_prefix):
                continue
            arm = dict(arm_value)
            matrix = arm_matrix(arm, len(features))
            arm["a_matrix"] = [
                [round((1.0 if row == column else 0.0)
                       + (matrix[row][column] - (1.0 if row == column else 0.0)) * shrink, 8)
                 for column in range(len(features))]
                for row in range(len(features))
            ]
            arm["b_vector"] = [round(float(value) * shrink, 8)
                               for value in arm.get("b_vector", [])]
            arm["count"] = min(int(arm.get("count") or 0),
                               max(0, int(learner.get("minimum_trials", 2)) - 1))
            arms[arm_key] = arm
        cumulative = 0.0
        maximum = 0.0
    drift.update({"count": drift_count, "mean_reward": round(drift_mean, 8),
                  "cumulative_sum": round(cumulative, 8),
                  "maximum_sum": round(maximum, 8),
                  "cumulative_drop": round(maximum - cumulative, 8),
                  "detections": int(drift.get("detections") or 0) + int(detected),
                  "detected": detected})
    if detected:
        drift["last_detected_at"] = iso(utcnow())
    drifts[tier] = drift
    request_learning.update({"arms": arms, "drifts": drifts, "updated_at": iso(utcnow())})
    updated = dict(state)
    updated["request_learning"] = request_learning
    return updated


def quality_from_result(result: Dict[str, Any]) -> float:
    """Read explicit evaluator output only; do not guess semantic quality."""
    for key in ("quality_score", "score", "reward"):
        if key in result:
            return clamp(float(result[key]))
    return 1.0 if result.get("success", True) else 0.0


def _contract_subset(actual: Any, expected: Any, path: str = "answer") -> List[str]:
    """Return deterministic contract violations; expected mappings are subsets."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object"]
        violations: List[str] = []
        for key, value in expected.items():
            child = f"{path}.{key}"
            if key not in actual:
                violations.append(f"{child}: missing")
            else:
                violations.extend(_contract_subset(actual[key], value, child))
        return violations
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected array"]
        if len(actual) != len(expected):
            return [f"{path}: expected {len(expected)} items, got {len(actual)}"]
        violations: List[str] = []
        for index, value in enumerate(expected):
            violations.extend(_contract_subset(actual[index], value, f"{path}[{index}]"))
        return violations
    return [] if actual == expected else [f"{path}: expected {expected!r}, got {actual!r}"]


def verify_quality_contract(result: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
    """Verify an answer without trusting model confidence or self-evaluation."""
    if not isinstance(contract, dict):
        return {"passed": False, "violations": ["quality_contract must be an object"]}
    kind = str(contract.get("type") or "json_subset")
    if kind != "json_subset":
        return {"passed": False, "violations": [f"unsupported contract type: {kind}"]}
    actual = result.get("answer") if isinstance(result, dict) else None
    violations = _contract_subset(actual, contract.get("expected"), "answer")
    return {"passed": not violations, "violations": violations, "type": kind}


def quality_contract_plan(request: Dict[str, Any], policy: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a verifier-gated progressive-compute plan, independent of bandit state."""
    contract = request.get("quality_contract")
    settings = policy.get("quality_contracts", {})
    if not settings.get("enabled", False) or not isinstance(contract, dict):
        return None
    profiles = {item["name"]: item for item in policy["profiles"]}
    configured_stages = settings.get("stages", ["economy", "standard"])
    normalized_stages = []
    for item in configured_stages:
        stage = {"profile": item} if isinstance(item, str) else dict(item)
        if stage.get("profile") in profiles:
            normalized_stages.append(stage)
    if not normalized_stages:
        normalized_stages = [{"profile": policy["default_profile"]}]
    maximum = max(1, int(settings.get("max_attempts", len(normalized_stages))))
    stages = []
    for index, configured in enumerate(normalized_stages[:maximum]):
        name = configured["profile"]
        profile = profiles[name]
        stage = {
            "stage": index + 1,
            "profile": name,
            "reasoning_effort": profile["reasoning_effort"],
            "verbosity": profile.get("verbosity", "low"),
            "reasoning_summary": profile.get("reasoning_summary", "none"),
            "context_window": profile["context_window"],
            "compact_token_limit": profile["compact_token_limit"],
        }
        if configured.get("model"):
            stage["model"] = str(configured["model"])
        stages.append(stage)
    return {
        "strategy": "verified-progressive-inference",
        "acceptance_source": "external-deterministic-contract",
        "contract": contract,
        "stages": stages,
        "stop_on_pass": True,
        "guarantee_scope": "quality is preserved only to the extent the supplied contract is sound",
    }


def execute_request(
    request: Dict[str, Any], policy: Dict[str, Any], state: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Plan, execute a JSONL-capable command, and learn from every outcome."""
    command = request.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ValueError("request requires command as a non-empty string array")
    plan = request_budget(request, policy, state.get("request_learning") or {})
    contract_plan = quality_contract_plan(request, policy)
    request_id = str(request.get("request_id") or uuid.uuid4())
    def run_attempt(attempt_plan: Dict[str, Any]) -> Dict[str, Any]:
        environment = dict(os.environ)
        environment["ELASTIC_BUDGET_PLAN"] = json.dumps(
            attempt_plan, ensure_ascii=False, separators=(",", ":"))
        environment["ELASTIC_REQUEST_ID"] = request_id
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                command, input=str(request.get("stdin") or ""), text=True,
                capture_output=True, env=environment,
                timeout=max(1.0, float(request.get("timeout_seconds") or 120)), check=False)
            return_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as error:
            timed_out, return_code = True, 124
            stdout = error.stdout if isinstance(error.stdout, str) else ""
            stderr = error.stderr if isinstance(error.stderr, str) else ""
        latency = time.monotonic() - started
        parsed: Dict[str, Any] = {}
        for line in reversed(stdout.splitlines()):
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate; break
        success = return_code == 0 and not timed_out and bool(parsed.get("success", True))
        usage = parsed.get("usage") or {}
        cost_tokens = float(parsed.get("cost_tokens") or (
            float(usage.get("input_tokens") or 0) + float(usage.get("output_tokens") or 0)
            + float(usage.get("reasoning_output_tokens") or 0)))
        feedback = {"tier": attempt_plan["tier"],
                    "profile": attempt_plan["budget_actions"]["profile"],
                    "features": attempt_plan["features"],
                    "quality_score": quality_from_result(parsed) if not timed_out else 0.0,
                    "cost_tokens": cost_tokens, "latency_seconds": latency, "success": success}
        archive_dir = request.get("output_archive_dir")
        stdout_compact = shrink_output(
            stdout, int(request.get("output_max_lines") or 160),
            int(request.get("output_max_chars") or 16000),
            Path(archive_dir) if archive_dir else None,
        )
        stderr_compact = shrink_output(
            stderr, int(request.get("output_max_lines") or 160),
            int(request.get("output_max_chars") or 16000),
            Path(archive_dir) if archive_dir else None,
        )
        return {"plan": attempt_plan, "feedback": feedback, "return_code": return_code,
                "timed_out": timed_out, "stdout": stdout, "stderr": stderr,
                "stdout_compact": stdout_compact, "stderr_compact": stderr_compact,
                "parsed_result": parsed}

    if contract_plan:
        attempts = []
        for stage in contract_plan["stages"]:
            attempt_plan = json.loads(json.dumps(plan))
            attempt_plan["strategy"] = contract_plan["strategy"]
            attempt_plan["budget_actions"].update(stage)
            attempt = run_attempt(attempt_plan)
            attempt["contract_verification"] = verify_quality_contract(
                attempt["parsed_result"], contract_plan["contract"])
            attempts.append(attempt)
            if attempt["feedback"]["success"] and attempt["contract_verification"]["passed"]:
                break
        final_attempt = attempts[-1]
        outcome = {
            "request_id": request_id, **final_attempt, "attempts": attempts,
            "cascade_triggered": len(attempts) > 1,
            "quality_contract": contract_plan,
            "contract_passed": bool(final_attempt["contract_verification"]["passed"]),
        }
        return outcome, state

    attempts = [run_attempt(plan)]
    first = attempts[0]
    cascade = plan.get("cascade") or {}
    quality_trigger = (
        first["feedback"]["quality_score"] < float(cascade.get("quality_below", 0.72))
        and float(plan["features"].get("quality_risk", 0.0)) >= float(cascade.get("minimum_quality_risk", 0.0))
    )
    should_fallback = bool(request.get("enable_cascade") and cascade.get("enabled") and (
        (cascade.get("fallback_on_failure") and not first["feedback"]["success"])
        or quality_trigger))
    if should_fallback and cascade.get("fallback_profile") != plan["budget_actions"]["profile"]:
        fallback_plan = json.loads(json.dumps(plan))
        fallback = next(item for item in policy["profiles"]
                        if item["name"] == cascade["fallback_profile"])
        fallback_plan["budget_actions"].update({
            "profile": fallback["name"], "reasoning_effort": fallback["reasoning_effort"],
            "verbosity": fallback.get("verbosity", "low"),
            "reasoning_summary": fallback.get("reasoning_summary", "concise"),
            "context_window": fallback["context_window"],
        })
        fallback_plan["learning_decision"] = "quality/failure cascade fallback"
        attempts.append(run_attempt(fallback_plan))
    updated = state
    for attempt in attempts:
        updated = update_request_learning(updated, attempt["feedback"], policy)
    final_attempt = attempts[-1]
    outcome = {
        "request_id": request_id,
        **final_attempt,
        "attempts": attempts,
        "cascade_triggered": len(attempts) > 1,
    }
    return outcome, updated


def request_budget(
    request: Dict[str, Any], policy: Dict[str, Any],
    learning: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Produce a non-empty, multi-dimensional budget decision for every request."""
    features = normalize_request(request)
    settings = policy.get("request_budgeting", {})
    bands = settings.get("bands") or [
        {"name": "micro", "max_tokens": 1024},
        {"name": "short", "max_tokens": 8192},
        {"name": "medium", "max_tokens": 24576},
        {"name": "long", "max_tokens": 65536},
        {"name": "ultra", "max_tokens": 10 ** 9},
    ]
    estimated = features["estimated_input_tokens"]
    token_pressure = clamp(estimated / max(1, int(settings.get("long_context_tokens", 65536))))
    tool_pressure = clamp(features["expected_tool_calls"] / 12.0)
    turn_pressure = clamp((features["expected_turns"] - 1) / 9.0)
    score = (
        0.35 * features["complexity"]
        + 0.25 * features["quality_risk"]
        + 0.20 * token_pressure
        + 0.10 * tool_pressure
        + 0.10 * turn_pressure
    )
    if features["latency_sensitive"]:
        score -= 0.12
    score = clamp(score)

    token_band = next(
        (index for index, band in enumerate(bands) if estimated <= int(band["max_tokens"])),
        len(bands) - 1,
    )
    score_band = 0 if score < 0.28 else 1 if score < 0.46 else 2 if score < 0.66 else 3
    tier_index = min(len(bands) - 1, max(token_band, score_band))
    tier = str(bands[tier_index]["name"])

    action_table = settings.get("actions") or {
        "micro": {"profile": "economy", "max_output_tokens": 800,
                  "context_mode": "stable-prefix", "compression_mode": "none"},
        "short": {"profile": "balanced", "max_output_tokens": 1600,
                  "context_mode": "stable-prefix", "compression_mode": "none"},
        "medium": {"profile": "standard", "max_output_tokens": 3200,
                   "context_mode": "selective", "compression_mode": "extractive"},
        "long": {"profile": "extended", "max_output_tokens": 4800,
                 "context_mode": "retrieve-and-rerank", "compression_mode": "semantic"},
        "ultra": {"profile": "extended", "max_output_tokens": 6400,
                  "context_mode": "hierarchical-memory", "compression_mode": "semantic"},
    }
    selected = dict(action_table.get(tier) or action_table["medium"])
    profiles = {item["name"]: item for item in policy["profiles"]}
    base_profile = selected.get("profile", policy["default_profile"])
    profile_name, learning_reason, propensity = choose_request_profile(
        tier, base_profile, request, policy, learning,
    )
    profile = profiles.get(profile_name, profiles[policy["default_profile"]])

    minimum = float(profile.get("compact_min_ratio", 0.58))
    maximum = float(profile.get("compact_max_ratio", 0.94))
    compact_ratio = minimum + (maximum - minimum) * (0.25 + 0.75 * token_pressure)
    compact_ratio = clamp(compact_ratio, minimum, maximum)
    quantum = max(1, int(policy.get("elastic_compaction", {}).get("quantum_tokens", 1024)))
    window = int(profile["context_window"])
    reserve = int(policy.get("elastic_compaction", {}).get("minimum_reserve_tokens", 8192))
    compact_limit = int(round(window * compact_ratio / quantum) * quantum)
    compact_limit = max(quantum, min(window - reserve, compact_limit))

    cache_mode = "stable-prefix-reuse" if features["reusable_prefix"] else "avoid-cache-write"
    selected.update({
        "profile": profile_name,
        "reasoning_effort": profile["reasoning_effort"],
        "verbosity": profile.get("verbosity", "low"),
        "reasoning_summary": profile.get("reasoning_summary", "concise"),
        "context_window": window,
        "compact_token_limit": compact_limit,
        "compact_ratio": round(compact_limit / window, 4),
        "cache_mode": cache_mode,
        "target_input_tokens": min(window - reserve, max(estimated, int(estimated * 1.2))),
    })
    context_target = min(
        selected["target_input_tokens"],
        max(1, int(estimated * float(settings.get("context_target_ratio", 0.72)))),
    )
    selection = select_context_segments(request, context_target, policy)
    layout = prompt_layout_plan(request, selection)
    candidates = list((settings.get("candidate_profiles") or {}).get(tier) or [profile_name])
    try:
        current_index = candidates.index(profile_name)
    except ValueError:
        current_index = 0
    fallback_profile = candidates[min(len(candidates) - 1, current_index + 1)] if candidates else profile_name
    cascade = settings.get("cascade", {})
    cascade_threshold = clamp(
        float(cascade.get("quality_below", 0.72))
        + float(cascade.get("quality_risk_adjustment", 0.08)) * features["quality_risk"],
        0.0, 1.0,
    )
    return {
        "policy_version": policy.get("version", 1),
        "decision_scope": "every_request",
        "tier": tier,
        "score": round(score, 4),
        "features": features,
        "budget_actions": selected,
        "learning_decision": learning_reason,
        "action_propensity": propensity,
        "context_selection": selection,
        "prompt_layout": layout,
        "cascade": {
            "enabled": bool(cascade.get("enabled", True)),
            "fallback_profile": fallback_profile,
            "quality_below": round(cascade_threshold, 4),
            "minimum_quality_risk": float(cascade.get("minimum_quality_risk", 0.35)),
            "fallback_on_failure": bool(cascade.get("fallback_on_failure", True)),
            "execution_requires_opt_in": True,
        },
    }


def item_failed(item: Dict[str, Any]) -> bool:
    """Use structured result fields; never classify arbitrary output text."""
    status = str(item.get("status") or "").lower()
    if status in {"failed", "error", "timed_out", "timeout", "cancelled"}:
        return True
    exit_code = item.get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


def session_activity(path: Path) -> Dict[str, Any]:
    """Return terminal/open-turn state without relying on file ordering."""
    open_turns: Dict[str, datetime] = {}
    last_event_at: Optional[datetime] = None
    completed_turns = aborted_turns = 0
    for _, event in iter_events([path]):
        timestamp = parse_time(event.get("timestamp"))
        if timestamp and (last_event_at is None or timestamp > last_event_at):
            last_event_at = timestamp
        payload = event.get("payload") or {}
        payload_type = payload.get("type")
        turn_id = str(payload.get("turn_id") or "")
        if event.get("type") == "event_msg" and payload_type == "task_started":
            open_turns[turn_id] = timestamp or utcnow()
        elif event.get("type") == "event_msg" and payload_type == "task_complete":
            completed_turns += 1
            open_turns.pop(turn_id, None)
        elif event.get("type") == "event_msg" and payload_type == "turn_aborted":
            aborted_turns += 1
            open_turns.pop(turn_id, None)
    return {
        "last_event_at": last_event_at,
        "open_turns": len(open_turns),
        "completed_turns": completed_turns,
        "aborted_turns": aborted_turns,
    }


def select_active_session(
    paths: List[Path], preferred: Optional[str], now: datetime, stale_minutes: int,
) -> Optional[Path]:
    if not paths:
        return None
    if preferred:
        preferred_path = Path(preferred)
        if preferred_path in paths and preferred_path.exists():
            activity = session_activity(preferred_path)
            last = activity.get("last_event_at")
            if last and now - last < timedelta(minutes=stale_minutes):
                return preferred_path
    return paths[0]


def session_is_evaluable(path: Path, now: datetime, stale_minutes: int) -> bool:
    activity = session_activity(path)
    last = activity.get("last_event_at")
    return bool(
        last and now - last >= timedelta(minutes=stale_minutes)
        and activity.get("open_turns", 0) == 0
        and activity.get("completed_turns", 0) > 0
    )


def session_outcome(path: Path, policy: Dict[str, Any]) -> Dict[str, Any]:
    totals = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    starts: Dict[str, datetime] = {}
    durations: List[float] = []
    compactions = failures = forgetting = completed_turns = 0
    for _, event in iter_events([path]):
        timestamp = parse_time(event.get("timestamp"))
        payload = event.get("payload") or {}
        payload_type = payload.get("type")
        turn_id = str(payload.get("turn_id") or "")
        if event.get("type") == "compacted":
            compactions += 1
        if event.get("type") == "event_msg" and payload_type == "task_started" and timestamp:
            starts[turn_id] = timestamp
        elif event.get("type") == "event_msg" and payload_type == "task_complete":
            completed_turns += 1
            if timestamp and turn_id in starts:
                durations.append((timestamp - starts[turn_id]).total_seconds())
        elif event.get("type") == "event_msg" and payload_type == "token_count":
            usage = ((payload.get("info") or {}).get("last_token_usage") or {})
            totals["input"] += int(usage.get("input_tokens") or 0)
            totals["cached"] += int(usage.get("cached_input_tokens") or 0)
            totals["output"] += int(usage.get("output_tokens") or 0)
            totals["reasoning"] += int(usage.get("reasoning_output_tokens") or 0)
        elif event.get("type") == "event_msg" and payload_type == "item_completed":
            item = payload.get("item") or {}
            text = json.dumps(item, ensure_ascii=False).lower()
            if item.get("type") == "UserMessage":
                forgetting += sum(1 for pattern in FORGETTING_PATTERNS if pattern in text)
            if item.get("type") in {"CommandExecution", "McpToolCall", "DynamicToolCall"}:
                failures += int(item_failed(item))

    uncached = max(0, totals["input"] - totals["cached"])
    weights = policy.get("learning", {}).get("cost_weights", {})
    cost = (
        uncached * float(weights.get("uncached_input", 1.0))
        + totals["cached"] * float(weights.get("cached_input", 0.2))
        + totals["output"] * float(weights.get("output", 4.0))
        + totals["reasoning"] * float(weights.get("reasoning", 6.0))
    )
    median_latency = statistics.median(durations) if durations else 0.0
    penalties = policy.get("learning", {}).get("penalties", {})
    completed_scale = max(1, completed_turns)
    quality_penalty = (
        compactions / completed_scale * float(penalties.get("compaction", 0.8))
        + failures / completed_scale * float(penalties.get("failure", 1.5))
        + forgetting / completed_scale * float(penalties.get("forgetting", 2.5))
        + (0 if completed_turns else float(penalties.get("no_completion", 1.0)))
    )
    cost_scale = max(1.0, float(policy.get("learning", {}).get("cost_scale_tokens", 100000)))
    latency_scale = max(1.0, float(policy.get("learning", {}).get("latency_scale_seconds", 120)))
    reward = (
        1.0 - quality_penalty
        - (cost / completed_scale) / cost_scale
        - median_latency / latency_scale
    )
    return {
        "reward": round(reward, 4), "cost_proxy": round(cost, 1),
        "median_latency_seconds": round(median_latency, 2),
        "compactions": compactions, "failures": failures,
        "forgetting_signals": forgetting, "completed_turns": completed_turns,
        "tokens": totals,
    }


def update_learning(
    state: Dict[str, Any], finished_session: Optional[str], policy: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    learning = dict(state.get("learning") or {})
    stats = dict(learning.get("strategies") or {})
    evaluated = list(learning.get("evaluated_sessions") or [])
    if not finished_session or finished_session in evaluated or not Path(finished_session).exists():
        learning["strategies"] = stats
        learning["evaluated_sessions"] = evaluated[-100:]
        return learning, None
    strategy = (state.get("session_strategies") or {}).get(finished_session)
    strategy = strategy or state.get("active_strategy") or state.get("current_profile")
    if not strategy:
        return learning, None
    outcome = session_outcome(Path(finished_session), policy)
    item = dict(stats.get(strategy) or {})
    count = int(item.get("count") or 0) + 1
    total_reward = float(item.get("total_reward") or 0.0) + outcome["reward"]
    item.update({
        "count": count, "total_reward": round(total_reward, 4),
        "mean_reward": round(total_reward / count, 4),
        "total_cost_proxy": round(float(item.get("total_cost_proxy") or 0.0) + outcome["cost_proxy"], 1),
        "compactions": int(item.get("compactions") or 0) + outcome["compactions"],
        "failures": int(item.get("failures") or 0) + outcome["failures"],
        "forgetting_signals": int(item.get("forgetting_signals") or 0) + outcome["forgetting_signals"],
        "last_outcome": outcome,
    })
    stats[strategy] = item
    evaluated.append(finished_session)
    learning.update({"strategies": stats, "evaluated_sessions": evaluated[-100:]})
    return learning, {"strategy": strategy, "session": finished_session, **outcome}


def choose_learning_strategy(
    policy: Dict[str, Any], learning: Dict[str, Any], pressure_score: int, session: str,
) -> Tuple[str, str]:
    profiles = policy["profiles"]
    names = [item["name"] for item in profiles]
    minimum_level = 2 if pressure_score >= 6 else 1 if pressure_score >= 3 else 0
    candidates = [item for item in profiles if int(item.get("capacity_level", 0)) >= minimum_level]
    stats = learning.get("strategies") or {}
    minimum_trials = int(policy.get("learning", {}).get("minimum_trials", 2))
    under_tested = [item for item in candidates if int((stats.get(item["name"]) or {}).get("count") or 0) < minimum_trials]
    if under_tested:
        seed = int(hashlib.sha256(session.encode()).hexdigest()[:8], 16)
        chosen = under_tested[seed % len(under_tested)]["name"]
        return chosen, "minimum-trial exploration"
    total = max(1, sum(int((stats.get(name) or {}).get("count") or 0) for name in names))
    exploration = float(policy.get("learning", {}).get("ucb_exploration", 0.7))
    def value(item: Dict[str, Any]) -> float:
        stat = stats.get(item["name"]) or {}
        count = max(1, int(stat.get("count") or 0))
        return float(stat.get("mean_reward") or 0.0) + exploration * math.sqrt(math.log(total + 1) / count)
    chosen = max(candidates, key=value)["name"]
    return chosen, "learned UCB selection"


def recent_session_files(directory: Path, now: datetime, hours: int) -> List[Path]:
    cutoff = now.timestamp() - hours * 3600
    try:
        files = [
            path for path in directory.rglob("*.jsonl")
            if path.stat().st_mtime >= cutoff
        ]
    except OSError:
        return []
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:8]


def iter_events(paths: Iterable[Path]) -> Iterable[Tuple[Path, Dict[str, Any]]]:
    for path in paths:
        try:
            with path.open(errors="replace") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield path, event
        except OSError:
            continue


def robust_growth_rate(samples: List[Tuple[datetime, int]]) -> float:
    """Estimate growth without treating post-compaction resets as new tokens."""
    positive = [(timestamp, tokens) for timestamp, tokens in samples if tokens > 0]
    if len(positive) < 2:
        return 0.0

    segments: List[List[Tuple[datetime, int]]] = []
    current: List[Tuple[datetime, int]] = []
    for sample in positive:
        if current and sample[1] < current[-1][1]:
            if len(current) >= 2:
                segments.append(current)
            current = []
        current.append(sample)
    if len(current) >= 2:
        segments.append(current)

    rates: List[float] = []
    for segment in segments[-6:]:
        minutes = (segment[-1][0] - segment[0][0]).total_seconds() / 60.0
        delta = segment[-1][1] - segment[0][1]
        if minutes >= 0.5 and delta > 0:
            rates.append(delta / minutes)
    return statistics.median(rates) if rates else 0.0


def collect_metrics(
    directory: Path, policy: Dict[str, Any], now: datetime,
    configured_context_window: Optional[int] = None,
    preferred_session: Optional[str] = None,
) -> Dict[str, Any]:
    paths = recent_session_files(directory, now, int(policy["lookback_hours"]))
    if not paths:
        return {
            "sessions": 0, "occupancy": 0.0, "growth_tokens_per_minute": 0.0,
            "compactions": 0, "min_compaction_gap_minutes": None,
            "cache_hit_rate": 0.0, "tool_calls": 0, "turns": 0,
            "forgetting_signals": 0, "legacy_session": False,
            "reported_context_window": 0, "window_ratio": None,
        }

    active = select_active_session(
        paths, preferred_session, now,
        int(policy.get("session_stale_minutes", 15)),
    )
    if active is None:
        raise RuntimeError("session selection failed")
    token_samples: List[Tuple[datetime, int, int, int]] = []
    compactions: List[datetime] = []
    tool_calls = turns = forgetting = 0
    window_start = now - timedelta(minutes=int(policy["compaction_window_minutes"]))

    for path, event in iter_events(paths):
        timestamp = parse_time(event.get("timestamp"))
        event_type = event.get("type")
        payload = event.get("payload") or {}
        if (
            path == active and event_type == "compacted"
            and timestamp and timestamp >= window_start
        ):
            compactions.append(timestamp)
        if path != active or event_type != "event_msg":
            continue
        payload_type = payload.get("type")
        if payload_type == "token_count" and timestamp:
            info = payload.get("info") or {}
            usage = info.get("last_token_usage") or {}
            token_samples.append((
                timestamp,
                int(usage.get("input_tokens") or 0),
                int(usage.get("cached_input_tokens") or 0),
                int(info.get("model_context_window") or 0),
            ))
        elif payload_type == "item_completed" and timestamp and timestamp >= window_start:
            item = payload.get("item") or {}
            kind = str(item.get("type") or "")
            if kind == "UserMessage":
                turns += 1
                lowered = json.dumps(item, ensure_ascii=False).lower()
                forgetting += sum(1 for pattern in FORGETTING_PATTERNS if pattern in lowered)
            elif kind in {
                "CommandExecution", "WebSearch", "McpToolCall", "DynamicToolCall",
                "ComputerUse", "FileChange", "SubAgentActivity",
            }:
                tool_calls += 1

    occupancy = cache_hit = growth = 0.0
    reported_context_window = 0
    if token_samples:
        _, current_input, current_cached, reported_context_window = token_samples[-1]
        context_window = configured_context_window or reported_context_window
        if context_window:
            occupancy = current_input / context_window
        if current_input:
            cache_hit = current_cached / current_input
        recent = [
            (timestamp, input_tokens)
            for timestamp, input_tokens, _, _ in token_samples
            if timestamp >= window_start
        ]
        growth = robust_growth_rate(recent)

    ratio = None
    legacy_session = False
    if configured_context_window and reported_context_window:
        ratio = reported_context_window / configured_context_window
        minimum = float(policy.get("session_window_ratio_min", 0.75))
        maximum = float(policy.get("session_window_ratio_max", 1.25))
        legacy_session = ratio < minimum or ratio > maximum

    compactions.sort()
    gaps = [
        (right - left).total_seconds() / 60.0
        for left, right in zip(compactions, compactions[1:])
    ]
    metrics = {
        "sessions": len(paths),
        "active_session": str(active),
        "occupancy": round(occupancy, 4),
        "growth_tokens_per_minute": round(growth, 1),
        "compactions": len(compactions),
        "min_compaction_gap_minutes": round(min(gaps), 1) if gaps else None,
        "cache_hit_rate": round(cache_hit, 4),
        "tool_calls": tool_calls,
        "turns": turns,
        "forgetting_signals": forgetting,
        "legacy_session": legacy_session,
        "reported_context_window": reported_context_window,
        "window_ratio": round(ratio, 4) if ratio is not None else None,
    }
    if legacy_session:
        metrics["ignored_legacy_metrics"] = {
            "occupancy": metrics["occupancy"],
            "growth_tokens_per_minute": metrics["growth_tokens_per_minute"],
            "compactions": metrics["compactions"],
            "min_compaction_gap_minutes": metrics["min_compaction_gap_minutes"],
            "tool_calls": metrics["tool_calls"],
            "turns": metrics["turns"],
            "forgetting_signals": metrics["forgetting_signals"],
        }
        metrics.update({
            "occupancy": 0.0,
            "growth_tokens_per_minute": 0.0,
            "compactions": 0,
            "min_compaction_gap_minutes": None,
            "tool_calls": 0,
            "turns": 0,
            "forgetting_signals": 0,
        })
    return metrics


def score_metrics(metrics: Dict[str, Any], policy: Dict[str, Any]) -> Tuple[int, List[str], bool]:
    if metrics.get("legacy_session"):
        return -2, [
            "legacy session window mismatch",
            "legacy pressure signals ignored",
        ], False
    limits = policy["signals"]
    score = 0
    reasons: List[str] = []
    emergency = False
    occupancy = metrics["occupancy"]
    if occupancy >= limits["occupancy_critical"]:
        score += 3; reasons.append("critical occupancy"); emergency = True
    elif occupancy >= limits["occupancy_high"]:
        score += 2; reasons.append("high occupancy")
    elif metrics["sessions"] and occupancy <= limits["occupancy_low"]:
        score -= 1; reasons.append("low occupancy")
    if metrics["growth_tokens_per_minute"] >= limits["growth_high_tokens_per_minute"]:
        score += 1; reasons.append("fast token growth")
    if metrics["compactions"] >= limits["frequent_compactions"]:
        score += 2; reasons.append("frequent compaction")
    elif metrics["compactions"] == 1:
        score += 1; reasons.append("recent compaction")
    gap = metrics["min_compaction_gap_minutes"]
    if gap is not None and gap < limits["rapid_compaction_minutes"]:
        score += 2; reasons.append("rapid re-compaction")
        if gap < 5:
            emergency = True
    if metrics["tool_calls"] >= limits["tool_calls_high"]:
        score += 1; reasons.append("tool-heavy task")
    if metrics["turns"] >= limits["turns_high"]:
        score += 1; reasons.append("long conversation")
    if metrics["forgetting_signals"]:
        score += min(2, metrics["forgetting_signals"])
        reasons.append("context-loss feedback")
    if metrics["cache_hit_rate"] >= limits["cache_hit_good"]:
        score -= 1; reasons.append("good prompt-cache reuse")
    if metrics["sessions"] and metrics["compactions"] == 0 and metrics["tool_calls"] < 4 and metrics["turns"] < 4:
        score -= 1; reasons.append("light task")
    return score, reasons, emergency


def choose_profile(
    current: str, score: int, emergency: bool, state: Dict[str, Any],
    policy: Dict[str, Any], now: datetime, legacy_session: bool = False,
    profile_change_locked: bool = False, allow_downgrade: bool = True,
    repeat_compaction: bool = False,
) -> Tuple[str, str]:
    profiles = policy["profiles"]
    names = [profile["name"] for profile in profiles]
    index = names.index(current) if current in names else names.index(policy["default_profile"])
    default_index = names.index(policy["default_profile"])
    if profile_change_locked:
        return names[index], "per-session profile change limit reached"
    if legacy_session:
        return names[index], "legacy session isolated; profile frozen"
    if repeat_compaction:
        target = len(names) - 1
        if target == index:
            return names[index], "repeat-compaction guard already at maximum"
        return names[target], "repeat-compaction guard selected maximum profile"
    limits = policy["signals"]
    direction = 0
    if score >= limits["upgrade_score"]:
        direction = 1
    elif score <= limits["downgrade_score"]:
        direction = -1
    if direction < 0 and not allow_downgrade:
        return names[index], "active-session downgrade deferred"
    if direction == 0:
        return names[index], "score inside hysteresis band"
    last_change = parse_time(state.get("last_change_at"))
    cooldown = policy["upgrade_cooldown_minutes"] if direction > 0 else policy["downgrade_cooldown_minutes"]
    if last_change and not emergency and now < last_change + timedelta(minutes=cooldown):
        return names[index], f"cooldown active ({cooldown}m)"
    target = max(0, min(len(names) - 1, index + direction))
    if target == index:
        return names[index], "already at policy boundary"
    return names[target], "single-step profile adjustment"


def append_decision(record: Dict[str, Any]) -> None:
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def run(args: argparse.Namespace) -> Dict[str, Any]:
    now = utcnow()
    policy = read_json(Path(args.policy), None)
    if not policy:
        raise SystemExit(f"invalid policy: {args.policy}")
    profiles = {profile["name"]: profile for profile in policy["profiles"]}
    state = read_json(Path(args.state), {})
    current = state.get("current_profile", policy["default_profile"])
    if current not in profiles:
        current = policy["default_profile"]

    config_path = Path(args.config)
    config_text = config_path.read_text()
    last_eval = parse_time(state.get("last_evaluated_at"))
    due = not last_eval or (now - last_eval).total_seconds() >= policy["evaluation_interval_seconds"]

    metrics = state.get("metrics") or {}
    score = int(state.get("score") or 0)
    reasons = state.get("reasons") or []
    decision_reason = "evaluation interval not reached"
    target = current
    emergency = False
    if due or args.force:
        previous_session = state.get("active_session") or (state.get("metrics") or {}).get("active_session")
        metrics = collect_metrics(
            Path(args.sessions_dir), policy, now,
            int(profiles[current]["context_window"]),
            previous_session,
        )
        score, reasons, emergency = score_metrics(metrics, policy)
        active_session = metrics.get("active_session")
        same_session = bool(active_session and active_session == previous_session)
        stale_minutes = int(policy.get("session_stale_minutes", 15))
        finished_session = None
        if (
            previous_session and active_session != previous_session
            and Path(previous_session).exists()
            and session_is_evaluable(Path(previous_session), now, stale_minutes)
        ):
            finished_session = previous_session
        learning, learned_outcome = update_learning(
            state, finished_session, policy,
        )
        profile_change_locked = bool(
            active_session and state.get("last_profile_change_session") == active_session
        )
        repeat_compaction = bool(
            metrics.get("compactions", 0) >= policy.get("repeat_compaction_count", 2)
            or (
                metrics.get("min_compaction_gap_minutes") is not None
                and metrics["min_compaction_gap_minutes"]
                < policy.get("repeat_compaction_guard_minutes", 15)
            )
        )
        if repeat_compaction:
            target, decision_reason = choose_profile(
                current, score, True, state, policy, now,
                bool(metrics.get("legacy_session")),
                profile_change_locked, not same_session, True,
            )
        elif active_session and not same_session and not metrics.get("legacy_session"):
            target, decision_reason = choose_learning_strategy(
                policy, learning, score, active_session,
            )
        else:
            target, decision_reason = choose_profile(
                current, score, emergency, state, policy, now,
                bool(metrics.get("legacy_session")),
                profile_change_locked, not same_session, repeat_compaction,
            )
    else:
        learning = dict(state.get("learning") or {})
        learned_outcome = None

    compact_token_limit, compact_ratio = elastic_compact_token_limit(
        profiles[target], metrics, policy,
    )
    wanted = desired_config(config_text, profiles[target], compact_token_limit)
    config_changed = wanted != config_text
    profile_changed = target != current
    record = {
        "timestamp": iso(now), "from": current, "to": target, "score": score,
        "reasons": reasons, "decision": decision_reason, "emergency": emergency,
        "config_changed": config_changed, "dry_run": args.dry_run, "metrics": metrics,
        "compact_token_limit": compact_token_limit, "compact_ratio": compact_ratio,
        "strategy": target, "learned_outcome": learned_outcome,
    }
    if args.dry_run:
        return record

    if config_changed:
        atomic_write(config_path, wanted)
    new_state = dict(state)
    session_strategies = dict(state.get("session_strategies") or {})
    if metrics.get("active_session"):
        session_strategies.setdefault(metrics["active_session"], target)
    if len(session_strategies) > 100:
        session_strategies = dict(list(session_strategies.items())[-100:])
    new_state.update({
        "version": 1, "current_profile": target, "score": score,
        "reasons": reasons, "metrics": metrics,
        "compact_token_limit": compact_token_limit, "compact_ratio": compact_ratio,
        "active_strategy": target, "learning": learning,
        "session_strategies": session_strategies,
    })
    if metrics.get("active_session"):
        new_state["active_session"] = metrics["active_session"]
    if due or args.force:
        new_state["last_evaluated_at"] = iso(now)
    if profile_changed:
        new_state["previous_profile"] = current
        new_state["last_change_at"] = iso(now)
        if metrics.get("active_session"):
            new_state["last_profile_change_session"] = metrics["active_session"]
    should_log = profile_changed or config_changed
    last_log = parse_time(state.get("last_log_at"))
    if due and (not last_log or now - last_log >= timedelta(minutes=10)):
        should_log = True
    if should_log:
        append_decision(record)
        new_state["last_log_at"] = iso(now)
    atomic_write(Path(args.state), json.dumps(new_state, ensure_ascii=False, indent=2) + "\n", 0o600)
    return record


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--dry-run", action="store_true")
    result.add_argument("--force", action="store_true")
    result.add_argument("--verbose", action="store_true")
    result.add_argument("--config", default=str(CONFIG))
    result.add_argument("--policy", default=str(POLICY))
    result.add_argument("--state", default=str(STATE))
    result.add_argument("--sessions-dir", default=str(SESSIONS))
    result.add_argument(
        "--plan-request", metavar="TEXT",
        help="emit an every-request preflight budget plan without changing config",
    )
    result.add_argument(
        "--request-json", metavar="JSON",
        help="request descriptor with text/tokens/complexity/tools/turns fields",
    )
    result.add_argument(
        "--record-feedback", metavar="JSON",
        help="update request learner with tier/profile/quality/cost/latency feedback",
    )
    result.add_argument(
        "--execute-request", metavar="JSON",
        help="plan, run command, parse its final JSON line, and automatically learn",
    )
    result.add_argument(
        "--shrink-output", action="store_true",
        help="read tool output from stdin and emit a deterministic compact view",
    )
    result.add_argument("--shrink-max-lines", type=int, default=160)
    result.add_argument("--shrink-max-chars", type=int, default=16000)
    result.add_argument("--shrink-archive-dir")
    return result


def main() -> None:
    args = parser().parse_args()
    if args.shrink_output:
        compact = shrink_output(
            sys.stdin.read(), args.shrink_max_lines,
            args.shrink_max_chars,
            Path(args.shrink_archive_dir) if args.shrink_archive_dir else None,
        )
        print(compact["text"], end="\n" if compact["text"] else "")
        return
    if args.execute_request is not None:
        policy = read_json(Path(args.policy), None)
        if not policy:
            raise SystemExit(f"invalid policy: {args.policy}")
        try:
            request = json.loads(args.execute_request)
            outcome, state = execute_request(request, policy, read_json(Path(args.state), {}))
        except (ValueError, TypeError) as error:
            raise SystemExit(f"invalid execute request JSON: {error}")
        atomic_write(Path(args.state), json.dumps(state, ensure_ascii=False, indent=2) + "\n", 0o600)
        feedback_log = Path(args.state).with_suffix(".requests.jsonl")
        feedback_log.parent.mkdir(parents=True, exist_ok=True)
        with feedback_log.open("a") as handle:
            handle.write(json.dumps(outcome, ensure_ascii=False, sort_keys=True) + "\n")
        print(json.dumps(outcome, ensure_ascii=False, indent=2))
        return
    if args.record_feedback is not None:
        policy = read_json(Path(args.policy), None)
        if not policy:
            raise SystemExit(f"invalid policy: {args.policy}")
        try:
            feedback = json.loads(args.record_feedback)
            state = update_request_learning(read_json(Path(args.state), {}), feedback, policy)
        except (ValueError, TypeError) as error:
            raise SystemExit(f"invalid feedback JSON: {error}")
        atomic_write(Path(args.state), json.dumps(state, ensure_ascii=False, indent=2) + "\n", 0o600)
        print(json.dumps({"updated": True, "request_learning": state["request_learning"]}, ensure_ascii=False, indent=2))
        return
    if args.plan_request is not None or args.request_json is not None:
        policy = read_json(Path(args.policy), None)
        if not policy:
            raise SystemExit(f"invalid policy: {args.policy}")
        try:
            request = json.loads(args.request_json) if args.request_json else {"text": args.plan_request}
        except ValueError as error:
            raise SystemExit(f"invalid request JSON: {error}")
        learning = (read_json(Path(args.state), {}).get("request_learning") or {})
        print(json.dumps(request_budget(request, policy, learning), ensure_ascii=False, indent=2))
        return
    result = run(args)
    if args.dry_run or args.verbose:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
