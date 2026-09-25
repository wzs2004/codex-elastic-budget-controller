#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "off_policy_eval", Path(__file__).parent / "benchmarks" / "off_policy_eval.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class OffPolicyEvalTests(unittest.TestCase):
    def test_matching_policy_reproduces_mean_reward(self):
        rows = [
            {"propensity": 0.5, "target_probability": 0.5, "reward": 1.0},
            {"propensity": 0.5, "target_probability": 0.5, "reward": 0.0},
        ]
        result = MODULE.evaluate(rows)
        self.assertAlmostEqual(result["ips"], 0.5)
        self.assertAlmostEqual(result["snips"], 0.5)
        self.assertAlmostEqual(result["doubly_robust"], 0.5)

    def test_rejects_zero_propensity_only_log(self):
        with self.assertRaises(ValueError):
            MODULE.evaluate([{"propensity": 0, "target_probability": 1, "reward": 1}])

    def test_reads_execute_request_log_shape(self):
        result = MODULE.evaluate([{
            "plan": {"action_propensity": 0.8}, "target_probability": 0.8,
            "feedback": {"reward": 0.7},
        }])
        self.assertAlmostEqual(result["snips"], 0.7)


if __name__ == "__main__":
    unittest.main()
