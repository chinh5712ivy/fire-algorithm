import unittest

import numpy as np

from fire_filter import Rule, compare, crop_features, measure_windows


class FilterTests(unittest.TestCase):
    def test_centroid_alignment_removes_translation(self):
        frame_a = np.zeros((32, 32, 3), np.uint8)
        frame_b = frame_a.copy()
        frame_a[10:16, 6:12] = 255
        frame_b[10:16, 17:23] = 255
        left = crop_features(frame_a, (0, 0, 32, 32))
        right = crop_features(frame_b, (0, 0, 32, 32))
        change = compare(left, right)
        self.assertAlmostEqual(change["hot_change"], 1)
        self.assertAlmostEqual(change["aligned_hot_change"], 0)
        self.assertAlmostEqual(change["area_change"], 0)

    def test_one_large_jump_does_not_confirm(self):
        rule = Rule(6, aligned_hot_change=0.10, area_change=0.067, edge_smoothness=0.85)
        calm = dict(hot_change=0, side_change=0, centered_change=0, brightness_change=0,
                    warm_change=0, aligned_hot_change=0.02, area_change=0.01, edge_smoothness=0.90)
        jump = {**calm, "aligned_hot_change": 1, "area_change": 1}
        self.assertEqual(rule.evaluate(measure_windows([calm] * 4 + [jump], 6)), "waiting")
        self.assertEqual(rule.evaluate(measure_windows([calm] * 5 + [jump], 6)),
                         "low_aligned_hot_change")

    def test_all_active_thresholds_are_inclusive(self):
        rule = Rule(6, aligned_hot_change=0.10, area_change=0.067, edge_smoothness=0.85)
        exact = dict(hot_change=0, side_change=0, centered_change=0, brightness_change=0,
                     warm_change=0, aligned_hot_change=0.10, area_change=0.067, edge_smoothness=0.85)
        self.assertEqual(rule.evaluate(measure_windows([exact] * 6, 6)), "pass")
        sharp = {**exact, "edge_smoothness": 0.82}
        self.assertEqual(rule.evaluate(measure_windows([sharp] * 6, 6)), "low_edge_smoothness")

    def test_empty_intersection_with_frame(self):
        frame = np.zeros((32, 32, 3), np.uint8)
        self.assertIsNone(crop_features(frame, (50, 50, 60, 60)))
