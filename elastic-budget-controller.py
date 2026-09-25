#!/usr/bin/python3
"""Adaptive Codex context/compaction budget controller.

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
import tempfile
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
    return result


def main() -> None:
    args = parser().parse_args()
    result = run(args)
    if args.dry_run or args.verbose:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
