import math
import unittest

from slopguard.crap import (
    DEFAULT_CRAP_THRESHOLD,
    aggregate_crap,
    crap_score,
    weighted_complexity,
)


class CrapTests(unittest.TestCase):
    def test_default_threshold(self):
        self.assertEqual(DEFAULT_CRAP_THRESHOLD, 30.0)

    def test_fully_covered_collapses_to_complexity(self):
        self.assertAlmostEqual(crap_score(5, 100), 5.0)

    def test_untested_is_quadratic(self):
        # comp^2 + comp at cov=0
        self.assertAlmostEqual(crap_score(5, 0), 25 + 5)

    def test_partial_coverage_rewards_sharply(self):
        # 50% coverage removes 87.5% of the quadratic term: 5^2 * 0.5^3 + 5
        self.assertAlmostEqual(crap_score(5, 50), 25 * 0.125 + 5)

    def test_clamps_inputs(self):
        self.assertEqual(crap_score(-3, 50), 0.0)
        self.assertAlmostEqual(crap_score(5, 250), 5.0)  # cov clamped to 100
        self.assertAlmostEqual(crap_score(5, -10), 30.0)  # cov clamped to 0

    def test_weighted_complexity_is_geometric_mean(self):
        self.assertAlmostEqual(weighted_complexity(4, 9), 6.0)
        self.assertEqual(weighted_complexity(5, 0), 0.0)
        self.assertEqual(weighted_complexity(-1, 5), 0.0)

    def test_aggregate(self):
        agg = aggregate_crap([3.0, 10.0, 1.0])
        self.assertEqual(agg.sum, 14.0)
        self.assertEqual(agg.max, 10.0)
        self.assertEqual(agg.method_count, 3)

    def test_aggregate_empty(self):
        agg = aggregate_crap([])
        self.assertEqual((agg.sum, agg.max, agg.method_count), (0.0, 0.0, 0))

    def test_result_never_nan(self):
        self.assertFalse(math.isnan(crap_score(0, 0)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
