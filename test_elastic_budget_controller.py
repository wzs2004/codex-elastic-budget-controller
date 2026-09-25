#!/usr/bin/python3
import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("elastic-budget-controller.py")
SPEC = importlib.util.spec_from_file_location("elastic_budget", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
POLICY = json.loads(Path(__file__).with_name("elastic-budget-policy.example.json").read_text())


class ElasticBudgetTests(unittest.TestCase):
    def write_events(self, directory, events):
        session = directory / "active.jsonl"
        session.write_text("".join(json.dumps(event) + "\n" for event in events))
        return session

    def test_every_request_gets_multidimensional_budget_actions(self):
        plan = MODULE.request_budget({"text": "把这句话改短。", "task_type": "rewrite"}, POLICY)
        self.assertEqual(plan["decision_scope"], "every_request")
        self.assertTrue(plan["budget_actions"])
        self.assertGreater(plan["action_propensity"], 0)
        for key in (
            "reasoning_effort", "verbosity", "max_output_tokens", "context_mode",
            "compression_mode", "compact_token_limit", "cache_mode",
        ):
            self.assertIn(key, plan["budget_actions"])

    def test_request_budget_expands_across_full_token_range(self):
        micro = MODULE.request_budget({
            "estimated_input_tokens": 300, "task_type": "classification",
            "complexity": 0.1, "quality_risk": 0.1,
        }, POLICY)
        ultra = MODULE.request_budget({
            "estimated_input_tokens": 120000, "task_type": "research",
            "complexity": 0.95, "quality_risk": 0.95, "expected_tool_calls": 20,
            "expected_turns": 12,
        }, POLICY)
        self.assertEqual(micro["tier"], "micro")
        self.assertEqual(ultra["tier"], "ultra")
        self.assertLess(
            micro["budget_actions"]["max_output_tokens"],
            ultra["budget_actions"]["max_output_tokens"],
        )
        self.assertNotEqual(
            micro["budget_actions"]["compression_mode"],
            ultra["budget_actions"]["compression_mode"],
        )

    def test_same_tier_still_has_continuous_compaction_adjustment(self):
        low = MODULE.request_budget({
            "estimated_input_tokens": 9000, "complexity": 0.5, "quality_risk": 0.5,
        }, POLICY)
        high = MODULE.request_budget({
            "estimated_input_tokens": 22000, "complexity": 0.5, "quality_risk": 0.5,
        }, POLICY)
        self.assertEqual(low["tier"], high["tier"])
        self.assertLess(
            low["budget_actions"]["compact_token_limit"],
            high["budget_actions"]["compact_token_limit"],
        )

    def test_request_feedback_updates_learning_and_changes_selection(self):
        state = {}
        for _ in range(3):
            state = MODULE.update_request_learning(state, {
                "tier": "micro", "profile": "economy", "quality_score": 0.95,
                "cost_tokens": 300, "latency_seconds": 1, "success": True,
            }, POLICY)
            state = MODULE.update_request_learning(state, {
                "tier": "micro", "profile": "balanced", "quality_score": 0.40,
                "cost_tokens": 900, "latency_seconds": 5, "success": True,
            }, POLICY)
        plan = MODULE.request_budget({
            "estimated_input_tokens": 300, "task_type": "classification",
            "complexity": 0.1, "quality_risk": 0.1,
        }, POLICY, state["request_learning"])
        self.assertEqual(plan["budget_actions"]["profile"], "economy")
        self.assertIn("LinUCB", plan["learning_decision"])
        self.assertIn("a_matrix", next(iter(state["request_learning"]["arms"].values())))

    def test_execute_request_closes_feedback_loop(self):
        request = {
            "estimated_input_tokens": 100, "complexity": 0.1, "quality_risk": 0.1,
            "command": ["python3", "-c", (
                "import json, os; p=json.loads(os.environ['ELASTIC_BUDGET_PLAN']); "
                "print(json.dumps({'success': bool(p['budget_actions']), 'quality_score': 0.9, "
                "'usage': {'input_tokens': 100, 'output_tokens': 20}}))"
            )],
        }
        outcome, state = MODULE.execute_request(request, POLICY, {})
        self.assertTrue(outcome["feedback"]["success"])
        self.assertEqual(outcome["feedback"]["cost_tokens"], 120)
        self.assertTrue(state["request_learning"]["arms"])

    def test_context_selector_keeps_instructions_and_relevant_evidence(self):
        request = {
            "text": "苹果公司的营收是多少？", "estimated_input_tokens": 1200,
            "context_segments": [
                {"text": "必须引用证据。", "tokens": 80, "role": "instruction", "stable": True},
                {"text": "香蕉价格下降。", "tokens": 300},
                {"text": "苹果公司营收增长 12%。", "tokens": 300},
                {"text": "附录：数据截至 2026 年。", "tokens": 80},
            ],
        }
        selected = MODULE.select_context_segments(request, 500, POLICY)
        self.assertTrue(selected["applied"])
        self.assertIn(0, selected["selected_indices"])
        self.assertIn(2, selected["selected_indices"])
        self.assertIn(3, selected["selected_indices"])
        self.assertNotIn(1, selected["selected_indices"])

    def test_prompt_layout_places_stable_segments_first(self):
        request = {"context_segments": [
            {"text": "rules", "stable": True}, {"text": "dynamic evidence"},
        ]}
        layout = MODULE.prompt_layout_plan(request, {"selected_indices": [0, 1]})
        self.assertEqual(layout["stable_prefix_indices"], [0])
        self.assertEqual(layout["dynamic_suffix_indices"], [1])

    def test_quality_guardrail_excludes_degraded_arm(self):
        learning = {"arms": {
            "micro:economy": {"count": 4, "quality_mean": 0.3, "failures": 0,
                              "latency_seconds_total": 4},
            "micro:balanced": {"count": 4, "quality_mean": 0.9, "failures": 0,
                               "latency_seconds_total": 8},
        }}
        plan = MODULE.request_budget({"estimated_input_tokens": 300,
                                      "complexity": 0.1, "quality_risk": 0.1},
                                     POLICY, learning)
        self.assertEqual(plan["budget_actions"]["profile"], "balanced")

    def test_every_plan_exposes_safe_cascade(self):
        plan = MODULE.request_budget({"estimated_input_tokens": 300}, POLICY)
        self.assertTrue(plan["cascade"]["enabled"])
        self.assertIn("fallback_profile", plan["cascade"])
        self.assertTrue(plan["cascade"]["execution_requires_opt_in"])

    def test_opt_in_cascade_retries_with_stronger_profile(self):
        request = {
            "estimated_input_tokens": 300, "complexity": 0.5, "quality_risk": 0.8,
            "enable_cascade": True,
            "command": ["python3", "-c", (
                "import json, os; p=json.loads(os.environ['ELASTIC_BUDGET_PLAN']); "
                "q=0.9 if p['budget_actions']['profile']=='standard' else 0.4; "
                "print(json.dumps({'success': True, 'quality_score': q, "
                "'usage': {'input_tokens': 100, 'output_tokens': 20}}))"
            )],
        }
        outcome, state = MODULE.execute_request(request, POLICY, {})
        self.assertTrue(outcome["cascade_triggered"])
        self.assertEqual(len(outcome["attempts"]), 2)
        self.assertEqual(outcome["feedback"]["quality_score"], 0.9)
        self.assertEqual(len(state["request_learning"]["arms"]), 2)

    def test_low_risk_quality_miss_does_not_pay_for_cascade(self):
        request = {
            "estimated_input_tokens": 300, "complexity": 0.1, "quality_risk": 0.1,
            "enable_cascade": True,
            "command": ["python3", "-c", (
                "import json; print(json.dumps({'success': True, 'quality_score': 0.4, "
                "'usage': {'input_tokens': 100, 'output_tokens': 20}}))"
            )],
        }
        outcome, _ = MODULE.execute_request(request, POLICY, {})
        self.assertFalse(outcome["cascade_triggered"])

    def test_quality_contract_accepts_exact_subset(self):
        result = {"answer": {"value": 7, "extra": "allowed"}}
        checked = MODULE.verify_quality_contract(
            result, {"type": "json_subset", "expected": {"value": 7}})
        self.assertTrue(checked["passed"])

    def test_quality_contract_rejects_wrong_nested_value(self):
        result = {"answer": {"route": ["A", "C"]}}
        checked = MODULE.verify_quality_contract(
            result, {"type": "json_subset", "expected": {"route": ["A", "B"]}})
        self.assertFalse(checked["passed"])
        self.assertIn("answer.route[1]", checked["violations"][0])

    def test_verified_progressive_inference_stops_after_contract_pass(self):
        request = {
            "estimated_input_tokens": 100,
            "quality_contract": {"type": "json_subset", "expected": {"value": 7}},
            "command": ["python3", "-c", (
                "import json; print(json.dumps({'success': True, 'answer': {'value': 7}, "
                "'usage': {'input_tokens': 10, 'output_tokens': 5}}))"
            )],
        }
        outcome, state = MODULE.execute_request(request, POLICY, {})
        self.assertTrue(outcome["contract_passed"])
        self.assertEqual(len(outcome["attempts"]), 1)
        self.assertEqual(outcome["plan"]["strategy"], "verified-progressive-inference")
        self.assertEqual(outcome["plan"]["budget_actions"]["model"], "gpt-6-sol")
        self.assertEqual(state, {})

    def test_verified_progressive_inference_escalates_on_contract_failure(self):
        request = {
            "estimated_input_tokens": 100,
            "quality_contract": {"type": "json_subset", "expected": {"value": 7}},
            "command": ["python3", "-c", (
                "import json, os; p=json.loads(os.environ['ELASTIC_BUDGET_PLAN']); "
                "v=7 if p['budget_actions']['profile']=='standard' else 6; "
                "print(json.dumps({'success': True, 'answer': {'value': v}}))"
            )],
        }
        outcome, _ = MODULE.execute_request(request, POLICY, {})
        self.assertTrue(outcome["contract_passed"])
        self.assertEqual(len(outcome["attempts"]), 2)
        self.assertEqual(outcome["attempts"][1]["plan"]["budget_actions"]["profile"], "standard")
        self.assertNotIn("model", outcome["attempts"][1]["plan"]["budget_actions"])

    def test_drift_detector_shrinks_stale_learning(self):
        policy = json.loads(json.dumps(POLICY))
        policy["request_budgeting"]["learning"]["drift_detection"].update({
            "minimum_observations": 3, "threshold": 0.2, "delta": 0.0,
        })
        state = {}
        for quality in (0.95, 0.95, 0.1):
            state = MODULE.update_request_learning(state, {
                "tier": "micro", "profile": "economy", "quality_score": quality,
                "cost_tokens": 100, "latency_seconds": 1, "success": True,
            }, policy)
        self.assertTrue(state["request_learning"]["drifts"]["micro"]["detected"])
        arm = state["request_learning"]["arms"]["micro:economy"]
        self.assertLess(arm["count"], 2)

    def test_pressure_moves_only_one_level(self):
        metrics = {
            "sessions": 1, "occupancy": 0.90, "growth_tokens_per_minute": 3000,
            "compactions": 3, "min_compaction_gap_minutes": 4,
            "cache_hit_rate": 0, "tool_calls": 15, "turns": 12,
            "forgetting_signals": 1,
        }
        score, _, emergency = MODULE.score_metrics(metrics, POLICY)
        target, _ = MODULE.choose_profile("balanced", score, emergency, {}, POLICY, MODULE.utcnow())
        self.assertEqual(target, "standard")

    def test_cooldown_blocks_non_emergency_upgrade(self):
        now = MODULE.utcnow()
        state = {"last_change_at": MODULE.iso(now - timedelta(minutes=2))}
        target, reason = MODULE.choose_profile("standard", 3, False, state, POLICY, now)
        self.assertEqual(target, "standard")
        self.assertIn("cooldown", reason)

    def test_calm_workload_can_step_down(self):
        metrics = {
            "sessions": 1, "occupancy": 0.12, "growth_tokens_per_minute": 0,
            "compactions": 0, "min_compaction_gap_minutes": None,
            "cache_hit_rate": 0, "tool_calls": 0, "turns": 1,
            "forgetting_signals": 0,
        }
        score, _, emergency = MODULE.score_metrics(metrics, POLICY)
        target, _ = MODULE.choose_profile("standard", score, emergency, {}, POLICY, MODULE.utcnow())
        self.assertEqual(target, "balanced")

    def test_config_update_preserves_unrelated_and_secret_lines(self):
        original = 'model = "example-model"\nmodel_context_window = 1\nexample_unrelated_setting = "preserve-me"\n'
        profile = next(item for item in POLICY["profiles"] if item["name"] == "standard")
        updated = MODULE.desired_config(original, profile)
        self.assertIn('example_unrelated_setting = "preserve-me"', updated)
        self.assertIn('model = "example-model"', updated)
        self.assertIn("model_context_window = 98304", updated)

    def test_elastic_threshold_rises_with_pressure(self):
        profile = next(item for item in POLICY["profiles"] if item["name"] == "standard")
        calm = {
            "occupancy": 0.10, "growth_tokens_per_minute": 0,
            "tool_calls": 0, "turns": 1, "compactions": 0,
        }
        busy = {
            "occupancy": 0.80, "growth_tokens_per_minute": 5000,
            "tool_calls": 20, "turns": 12, "compactions": 0,
        }
        calm_limit, calm_ratio = MODULE.elastic_compact_token_limit(profile, calm, POLICY)
        busy_limit, busy_ratio = MODULE.elastic_compact_token_limit(profile, busy, POLICY)
        self.assertLess(calm_limit, busy_limit)
        self.assertLess(calm_ratio, busy_ratio)
        self.assertEqual(calm_limit % 1024, 0)
        self.assertEqual(busy_limit % 1024, 0)

    def test_repeat_compaction_uses_maximum_elastic_ratio(self):
        profile = next(item for item in POLICY["profiles"] if item["name"] == "standard")
        limit, ratio = MODULE.elastic_compact_token_limit(
            profile, {"compactions": 2}, POLICY,
        )
        self.assertEqual(limit, 88064)
        self.assertAlmostEqual(ratio, 88064 / 98304, places=4)

    def test_learning_explores_under_tested_safe_strategies(self):
        learning = {"strategies": {
            "standard": {"count": 2, "mean_reward": 0.5},
            "extended": {"count": 0, "mean_reward": 0.0},
        }}
        chosen, reason = MODULE.choose_learning_strategy(
            POLICY, learning, 5, "session-a",
        )
        self.assertEqual(chosen, "extended")
        self.assertIn("exploration", reason)

    def test_learning_updates_completed_session_once(self):
        now = MODULE.utcnow()
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "finished.jsonl"
            events = [
                {"timestamp": MODULE.iso(now - timedelta(seconds=20)), "type": "event_msg",
                 "payload": {"type": "task_started", "turn_id": "t1"}},
                {"timestamp": MODULE.iso(now - timedelta(seconds=10)), "type": "event_msg",
                 "payload": {"type": "token_count", "info": {"last_token_usage": {
                     "input_tokens": 10000, "cached_input_tokens": 8000,
                     "output_tokens": 500, "reasoning_output_tokens": 100}}}},
                {"timestamp": MODULE.iso(now), "type": "event_msg",
                 "payload": {"type": "task_complete", "turn_id": "t1"}},
            ]
            session.write_text("".join(json.dumps(event) + "\n" for event in events))
            state = {"active_strategy": "standard"}
            learning, outcome = MODULE.update_learning(state, str(session), POLICY)
            self.assertIsNotNone(outcome)
            self.assertEqual(learning["strategies"]["standard"]["count"], 1)
            learning2, outcome2 = MODULE.update_learning(
                {"active_strategy": "standard", "learning": learning}, str(session), POLICY,
            )
            self.assertIsNone(outcome2)
            self.assertEqual(learning2["strategies"]["standard"]["count"], 1)

    def test_successful_command_text_error_is_not_failure(self):
        now = MODULE.utcnow()
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "finished.jsonl"
            events = [
                {"timestamp": MODULE.iso(now - timedelta(seconds=2)), "type": "event_msg",
                 "payload": {"type": "item_completed", "item": {"type": "CommandExecution",
                 "status": "completed", "exit_code": 0, "aggregated_output": "error is documented"}}},
                {"timestamp": MODULE.iso(now), "type": "event_msg",
                 "payload": {"type": "task_complete", "turn_id": "t1"}},
            ]
            session.write_text("".join(json.dumps(event) + "\n" for event in events))
            self.assertEqual(MODULE.session_outcome(session, POLICY)["failures"], 0)

    def test_nonzero_command_exit_is_failure(self):
        self.assertTrue(MODULE.item_failed({
            "type": "CommandExecution", "status": "completed", "exit_code": 2,
        }))
        self.assertTrue(MODULE.item_failed({"type": "McpToolCall", "status": "failed"}))

    def test_unfinished_preferred_session_is_not_displaced(self):
        now = MODULE.utcnow()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            preferred = directory / "preferred.jsonl"
            newer = directory / "newer.jsonl"
            preferred.write_text(json.dumps({
                "timestamp": MODULE.iso(now - timedelta(minutes=2)), "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "open"},
            }) + "\n")
            newer.write_text(json.dumps({
                "timestamp": MODULE.iso(now - timedelta(minutes=1)), "type": "event_msg",
                "payload": {"type": "token_count", "info": {}},
            }) + "\n")
            import os
            os.utime(preferred, ((now - timedelta(minutes=2)).timestamp(),) * 2)
            os.utime(newer, ((now - timedelta(minutes=1)).timestamp(),) * 2)
            paths = MODULE.recent_session_files(directory, now, 6)
            selected = MODULE.select_active_session(paths, str(preferred), now, 15)
            self.assertEqual(selected, preferred)

    def test_stale_completed_preferred_session_can_switch(self):
        now = MODULE.utcnow()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            preferred = directory / "preferred.jsonl"
            newer = directory / "newer.jsonl"
            preferred.write_text("".join(json.dumps(event) + "\n" for event in [
                {"timestamp": MODULE.iso(now - timedelta(minutes=25)), "type": "event_msg",
                 "payload": {"type": "task_started", "turn_id": "done"}},
                {"timestamp": MODULE.iso(now - timedelta(minutes=20)), "type": "event_msg",
                 "payload": {"type": "task_complete", "turn_id": "done"}},
            ]))
            newer.write_text(json.dumps({
                "timestamp": MODULE.iso(now - timedelta(minutes=1)), "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "new"},
            }) + "\n")
            import os
            os.utime(preferred, ((now - timedelta(minutes=20)).timestamp(),) * 2)
            os.utime(newer, ((now - timedelta(minutes=1)).timestamp(),) * 2)
            paths = MODULE.recent_session_files(directory, now, 6)
            self.assertEqual(MODULE.select_active_session(paths, str(preferred), now, 15), newer)
            self.assertTrue(MODULE.session_is_evaluable(preferred, now, 15))

    def test_session_strategy_mapping_controls_attribution(self):
        now = MODULE.utcnow()
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary) / "finished.jsonl"
            session.write_text("".join(json.dumps(event) + "\n" for event in [
                {"timestamp": MODULE.iso(now - timedelta(seconds=2)), "type": "event_msg",
                 "payload": {"type": "task_started", "turn_id": "t"}},
                {"timestamp": MODULE.iso(now), "type": "event_msg",
                 "payload": {"type": "task_complete", "turn_id": "t"}},
            ]))
            state = {"active_strategy": "extended", "session_strategies": {str(session): "balanced"}}
            learning, outcome = MODULE.update_learning(state, str(session), POLICY)
            self.assertEqual(outcome["strategy"], "balanced")
            self.assertIn("balanced", learning["strategies"])

    def test_reward_normalizes_session_length(self):
        now = MODULE.utcnow()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            def make(name, turns):
                events = []
                for index in range(turns):
                    turn = str(index)
                    events.extend([
                        {"timestamp": MODULE.iso(now + timedelta(seconds=index * 3)),
                         "type": "event_msg", "payload": {"type": "task_started", "turn_id": turn}},
                        {"timestamp": MODULE.iso(now + timedelta(seconds=index * 3 + 1)),
                         "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {
                         "input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 100}}}},
                        {"timestamp": MODULE.iso(now + timedelta(seconds=index * 3 + 2)),
                         "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}},
                    ])
                path = directory / name
                path.write_text("".join(json.dumps(event) + "\n" for event in events))
                return path
            one = MODULE.session_outcome(make("one.jsonl", 1), POLICY)["reward"]
            four = MODULE.session_outcome(make("four.jsonl", 4), POLICY)["reward"]
            self.assertAlmostEqual(one, four, places=4)

    def test_growth_ignores_zero_and_reset_samples(self):
        base = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
        samples = [
            (base, 20000),
            (base + timedelta(minutes=1), 22000),
            (base + timedelta(minutes=1, seconds=1), 0),
            (base + timedelta(minutes=1, seconds=5), 19000),
            (base + timedelta(minutes=2, seconds=5), 21000),
        ]
        self.assertEqual(MODULE.robust_growth_rate(samples), 2000)

    def test_metrics_use_only_active_session_and_configured_window(self):
        now = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
        def event(timestamp, event_type, payload=None):
            result = {"timestamp": MODULE.iso(timestamp), "type": event_type}
            if payload is not None:
                result["payload"] = payload
            return result
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            old = directory / "old.jsonl"
            old.write_text(json.dumps(event(now - timedelta(minutes=2), "compacted")) + "\n")
            active_events = [
                event(now - timedelta(minutes=3), "event_msg", {
                    "type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 20000, "cached_input_tokens": 10000},
                        "model_context_window": 90000,
                    },
                }),
                event(now - timedelta(minutes=2), "compacted"),
                event(now - timedelta(minutes=1), "event_msg", {
                    "type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 24000, "cached_input_tokens": 12000},
                        "model_context_window": 90000,
                    },
                }),
            ]
            active = self.write_events(directory, active_events)
            old_time = (now - timedelta(minutes=10)).timestamp()
            active_time = now.timestamp()
            import os
            os.utime(old, (old_time, old_time))
            os.utime(active, (active_time, active_time))
            metrics = MODULE.collect_metrics(directory, POLICY, now, 98304)
        self.assertEqual(metrics["compactions"], 1)
        self.assertAlmostEqual(metrics["occupancy"], 24000 / 98304, places=4)
        self.assertFalse(metrics["legacy_session"])

    def test_legacy_session_freezes_current_profile(self):
        now = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
        def event(timestamp, event_type, payload=None):
            result = {"timestamp": MODULE.iso(timestamp), "type": event_type}
            if payload is not None:
                result["payload"] = payload
            return result
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            events = [
                event(now - timedelta(minutes=4), "event_msg", {
                    "type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 20000},
                        "model_context_window": 31129,
                    },
                }),
                event(now - timedelta(minutes=3), "compacted"),
                event(now - timedelta(minutes=2), "compacted"),
                event(now - timedelta(minutes=1), "event_msg", {
                    "type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": 26000},
                        "model_context_window": 31129,
                    },
                }),
            ]
            self.write_events(directory, events)
            metrics = MODULE.collect_metrics(directory, POLICY, now, 131072)

        self.assertTrue(metrics["legacy_session"])
        self.assertEqual(metrics["compactions"], 0)
        self.assertEqual(metrics["growth_tokens_per_minute"], 0)
        self.assertEqual(metrics["ignored_legacy_metrics"]["compactions"], 2)
        score, _, emergency = MODULE.score_metrics(metrics, POLICY)
        target, reason = MODULE.choose_profile(
            "extended", score, emergency,
            {"last_change_at": MODULE.iso(now)}, POLICY, now, True,
        )
        self.assertEqual(target, "extended")
        self.assertIn("profile frozen", reason)

    def test_repeat_compaction_jumps_to_maximum_once(self):
        now = MODULE.utcnow()
        target, reason = MODULE.choose_profile(
            "balanced", 6, True, {}, POLICY, now,
            repeat_compaction=True,
        )
        self.assertEqual(target, "extended")
        self.assertIn("repeat-compaction guard", reason)

        target, reason = MODULE.choose_profile(
            "extended", 6, True, {}, POLICY, now,
            profile_change_locked=True, repeat_compaction=True,
        )
        self.assertEqual(target, "extended")
        self.assertIn("per-session", reason)

    def test_same_session_downgrade_is_deferred(self):
        target, reason = MODULE.choose_profile(
            "standard", -2, False, {}, POLICY, MODULE.utcnow(),
            allow_downgrade=False,
        )
        self.assertEqual(target, "standard")
        self.assertIn("deferred", reason)

    def test_integration_repeat_compaction_changes_profile_only_once(self):
        now = MODULE.utcnow()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "sessions"
            sessions.mkdir()
            config = root / "config.toml"
            config.write_text(
                'model_provider = "custom"\nmodel = "example-model"\n'
                'model_reasoning_effort = "medium"\n'
                'model_context_window = 98304\n'
                'model_auto_compact_token_limit = 86016\n'
            )
            policy = root / "policy.json"
            policy.write_text(json.dumps(POLICY))
            state = root / "state.json"
            state.write_text(json.dumps({"current_profile": "standard"}))
            events = [
                {"timestamp": MODULE.iso(now - timedelta(minutes=4)),
                 "type": "event_msg", "payload": {"type": "token_count",
                 "info": {"last_token_usage": {"input_tokens": 70000},
                 "model_context_window": 98304}}},
                {"timestamp": MODULE.iso(now - timedelta(minutes=3)), "type": "compacted"},
                {"timestamp": MODULE.iso(now - timedelta(minutes=2)), "type": "compacted"},
                {"timestamp": MODULE.iso(now - timedelta(minutes=1)),
                 "type": "event_msg", "payload": {"type": "token_count",
                 "info": {"last_token_usage": {"input_tokens": 76000},
                 "model_context_window": 98304}}},
            ]
            session = self.write_events(sessions, events)
            decisions = root / "decisions.jsonl"
            original_decisions = MODULE.DECISIONS
            MODULE.DECISIONS = decisions
            args = Namespace(
                dry_run=False, force=True, verbose=False, config=str(config),
                policy=str(policy), state=str(state), sessions_dir=str(sessions),
            )
            try:
                first = MODULE.run(args)
                second = MODULE.run(args)
            finally:
                MODULE.DECISIONS = original_decisions

            self.assertEqual(first["to"], "extended")
            self.assertIn("repeat-compaction guard", first["decision"])
            self.assertEqual(second["to"], "extended")
            self.assertIn("per-session", second["decision"])
            saved = json.loads(state.read_text())
            self.assertEqual(saved["last_profile_change_session"], str(session))
            self.assertIn("model_auto_compact_token_limit = 122880", config.read_text())


if __name__ == "__main__":
    unittest.main()
