"""Unit tests for analysis calculations using temporary illustrative data only.
These numbers are software fixtures, not experimental findings and are not saved as paper results.
"""
import sys
import tempfile
import unittest
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import analyze_results

class AnalysisTests(unittest.TestCase):
    def test_jain_fairness_equal_values_is_one(self):
        df = pd.DataFrame([
            {"run_id": "r1", "test_type": "mixed_fairness", "repetition": 1, "throughput_mbps": 10.0},
            {"run_id": "r1", "test_type": "mixed_fairness", "repetition": 1, "throughput_mbps": 10.0},
            {"run_id": "r1", "test_type": "mixed_fairness", "repetition": 1, "throughput_mbps": 10.0},
        ])
        out = analyze_results.calculate_fairness(df)
        self.assertAlmostEqual(out.iloc[0]["jain_fairness_index"], 1.0)

    def test_jain_fairness_decreases_for_unequal_values(self):
        df = pd.DataFrame([
            {"run_id": "r1", "test_type": "mixed_fairness", "repetition": 1, "throughput_mbps": 20.0},
            {"run_id": "r1", "test_type": "mixed_fairness", "repetition": 1, "throughput_mbps": 10.0},
            {"run_id": "r1", "test_type": "mixed_fairness", "repetition": 1, "throughput_mbps": 10.0},
        ])
        out = analyze_results.calculate_fairness(df)
        self.assertGreater(out.iloc[0]["jain_fairness_index"], 0.0)
        self.assertLess(out.iloc[0]["jain_fairness_index"], 1.0)

    def test_mean_ci_reports_sample_count(self):
        out = analyze_results.mean_ci(pd.Series([1.0, 2.0, 3.0]))
        self.assertEqual(out["n"], 3)
        self.assertEqual(out["mean"], 2.0)
        self.assertLess(out["ci95_low"], out["mean"])
        self.assertGreater(out["ci95_high"], out["mean"])

if __name__ == "__main__":
    unittest.main()
