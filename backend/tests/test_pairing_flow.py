"""两班交表、最长重叠配对、并列相反判定与性能的标准库测试。

在 backend 目录运行：
    python -I -m unittest discover -s tests -v
"""

import time
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import mean, stdev
import math
import pathlib
import sys

# 兼容 ``python -I -m unittest discover -s tests``（隔离模式不会把 cwd 放上 sys.path）。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.services import domain
from src.services.domain import analyze_tables, compute
from src.services.pairing import (
    MIN_OVERLAP_MINUTES,
    overlap_minutes,
    pair_windows,
)

FMT = "%Y-%m-%d %H:%M"


def window(day, hour, minute=0, length_minutes=60):
    start = datetime(2026, 11, day, hour, minute)
    return start, start + timedelta(minutes=length_minutes)


def baseline_station(index, start, loss=8.0):
    return {
        "station_id": f"HX-{index:02d}", "record_id": f"B{index}",
        "window_start": start.strftime(FMT),
        "window_end": (start + timedelta(minutes=60)).strftime(FMT),
        "baseline_loss_kw": loss,
    }


def trial_record(station, record_id, start, loss, length=60):
    return {
        "station_id": station, "record_id": record_id,
        "window_start": start.strftime(FMT),
        "window_end": (start + timedelta(minutes=length)).strftime(FMT),
        "trial_loss_kw": loss,
    }


def tied_station_tables(index, low_diff, high_diff, day=12, hour=8):
    """一个原垫窗 08:00-09:00，两个试装窗各重叠 40 分钟，形成并列。"""
    start = datetime(2026, 11, day, hour)
    station = f"HX-{index:02d}"
    base = baseline_station(index, start)
    t_low = trial_record(station, f"T{index}L", start + timedelta(minutes=20), 8.0 + low_diff)
    t_high = trial_record(station, f"T{index}H", start - timedelta(minutes=20), 8.0 + high_diff)
    return base, [t_low, t_high]


def conflict_tables(count=30):
    wave = (-18, 12, 48, -33, 25, 7, -42, 31, -12, 58, -5, 23,
            9, -27, 40, -16, 35, -8, 14, 51, -36, 17, -22, 4,
            29, -45, 11, 46, -13, 5)
    bases, trials = [], []
    for i in range(1, count + 1):
        low = wave[(i - 1) % len(wave)] / 100.0 * 0.5
        start = datetime(2026, 11, 12, 8) + timedelta(hours=(i % 10), days=(i // 10))
        station = f"HX-{i:02d}"
        base = baseline_station(i, start)
        pair_trials = [
            trial_record(station, f"T{i}L", start + timedelta(minutes=20), 8.0 + low),
            trial_record(station, f"T{i}H", start - timedelta(minutes=20), 8.0 + low + 0.55),
        ]
        bases.append(base)
        trials.extend(pair_trials)
    return bases, trials


class OverlapMathTest(unittest.TestCase):
    def test_overlap_and_boundary_is_inclusive_for_30_minutes(self):
        a0, a1 = datetime(2026, 11, 12, 8), datetime(2026, 11, 12, 9)
        self.assertEqual(overlap_minutes(a0, a1, datetime(2026, 11, 12, 8, 30),
                                         datetime(2026, 11, 12, 10)), 30.0)
        self.assertLess(overlap_minutes(a0, a1, datetime(2026, 11, 12, 8, 31),
                                        datetime(2026, 11, 12, 10)), 30.0)
        self.assertEqual(overlap_minutes(a0, a1, datetime(2026, 11, 12, 10),
                                         datetime(2026, 11, 12, 11)), 0.0)
        self.assertEqual(MIN_OVERLAP_MINUTES, 30)


class PairSelectionTest(unittest.TestCase):
    def test_only_longest_pair_is_chosen(self):
        start = datetime(2026, 11, 12, 8)
        base = [baseline_station(1, start)]
        trials = [
            trial_record("HX-01", "short", start + timedelta(minutes=45), 8.0),   # 15 min
            trial_record("HX-01", "long", start + timedelta(minutes=10), 7.9),    # 50 min
        ]
        result = pair_windows(base, trials)
        self.assertEqual(result["paired_station_count"], 1)
        self.assertFalse(result["tie_stations"])
        self.assertEqual(result["plans"][0][0]["trial_record_id"], "long")

    def test_below_half_hour_is_unpaired(self):
        start = datetime(2026, 11, 12, 8)
        base = [baseline_station(1, start)]
        trials = [trial_record("HX-01", "short", start + timedelta(minutes=45), 8.0)]
        result = pair_windows(base, trials)
        self.assertEqual(result["paired_station_count"], 0)
        self.assertEqual(result["unpaired_stations"][0]["station_id"], "HX-01")

    def test_two_longest_ties_are_kept(self):
        base, trials = tied_station_tables(1, -0.2, 0.4)
        result = pair_windows([base], trials)
        self.assertEqual(result["tie_stations"], ["HX-01"])
        self.assertEqual(result["stations"][0]["tie_count"], 2)
        record_ids = {opt["trial_record_id"] for opt in result["stations"][0]["options"]}
        self.assertEqual(record_ids, {"T1L", "T1H"})
        self.assertEqual(len(result["plan_a"]), 1)
        self.assertEqual(len(result["plan_b"]), 1)
        self.assertNotEqual(result["plan_a"][0]["trial_record_id"],
                            result["plan_b"][0]["trial_record_id"])

    def test_station_matching_is_case_insensitive(self):
        start = datetime(2026, 11, 12, 8)
        base = [{**baseline_station(1, start), "station_id": "hx-01"}]
        trials = [trial_record("HX-01", "T1", start + timedelta(minutes=10), 7.9)]
        result = pair_windows(base, trials)
        self.assertEqual(result["paired_station_count"], 1)

    def test_chinese_headers_are_supported(self):
        base_rows = [{
            "换热站编号": "HX-09", "记录编号": "B9",
            "窗口起点": "2026-11-12 08:00", "窗口终点": "2026-11-12 09:00",
            "原垫平均热损（kW）": "8.12",
        }]
        trial_rows = [{
            "站点": "HX-09", "记录号": "T9",
            "开始时间": "2026-11-12 08:10", "结束时间": "2026-11-12 09:10",
            "试装垫平均热损（kW）": "7.94",
        }]
        result = pair_windows(base_rows, trial_rows)
        self.assertEqual(result["paired_station_count"], 1)
        self.assertAlmostEqual(result["plan_a"][0]["baseline_loss_kw"], 8.12)


class AcceptanceTest(unittest.TestCase):
    def test_original_paired_csv_result_unchanged(self):
        rows = domain.sample_rows()
        result = compute(rows, {"min_pairs": 12})
        differences = [float(Decimal(str(r["trial_loss_kw"])) - Decimal(str(r["baseline_loss_kw"])))
                       for r in rows]
        expected_upper = (mean(differences)
                          + domain._critical_t(len(differences) - 1)
                          * stdev(differences) / math.sqrt(len(differences)))
        self.assertAlmostEqual(result["metrics"][domain.UPPER_METRIC], expected_upper, places=12)
        self.assertAlmostEqual(result["metrics"][domain.UPPER_METRIC], 0.14928707, places=8)
        self.assertTrue(result["can_publish"])

    def test_standard_shift_tables_reproduce_registered_result(self):
        baseline, trial = domain.sample_shift_tables()
        result = analyze_tables(baseline, trial, {"min_pairs": 12})
        self.assertEqual(result["tie_count"], 0)
        self.assertAlmostEqual(result["metrics"][domain.UPPER_METRIC], 0.14928707, places=8)
        self.assertTrue(result["can_publish"])

    def test_opposite_verdicts_pend_and_list_two_pairings(self):
        bases, trials = conflict_tables(30)
        result = analyze_tables(bases, trials, {"min_pairs": 12})
        self.assertTrue(result["tie_conflict"])
        self.assertFalse(result["can_publish"])
        self.assertIn("暂缓", result["headline"])
        plans = result["pairing_plans"]
        self.assertEqual(len(plans), 2)
        self.assertNotEqual(plans[0]["can_publish"], plans[1]["can_publish"])
        self.assertEqual(len(plans[0]["rows"]), 30)
        self.assertEqual(len(plans[1]["rows"]), 30)
        # 近义键都指向两套配对，方便分析页/导出取用。
        self.assertEqual(len(result["alternative_pairings"]), 2)
        self.assertEqual(len(result["pairing_sets"]), 2)

    def test_thirty_station_ties_decide_under_one_minute(self):
        bases, trials = conflict_tables(30)
        started = time.perf_counter()
        result = analyze_tables(bases, trials, {"min_pairs": 12})
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 60.0)
        self.assertTrue(result["tie_conflict"])

    def test_agreeing_ties_do_not_pend(self):
        bases, trials = [], []
        wave = (4, -6, 8, -2, 10, -8, 6, -4, 12, 2, -10, 8) * 3
        for i in range(1, 31):
            low = wave[i - 1] / 100.0
            start = datetime(2026, 11, 12, 8) + timedelta(hours=(i % 10), days=(i // 10))
            bases.append(baseline_station(i, start))
            station = f"HX-{i:02d}"
            trials.append(trial_record(station, f"T{i}L",
                                        start + timedelta(minutes=20), 8.0 + low))
            trials.append(trial_record(station, f"T{i}H",
                                        start - timedelta(minutes=20), 8.0 + low + 0.01))
        result = analyze_tables(bases, trials, {"min_pairs": 12})
        self.assertFalse(result["tie_conflict"])
        self.assertTrue(result["can_publish"])

    def test_tables_must_both_exist(self):
        with self.assertRaises(ValueError):
            analyze_tables(None, [], {})


if __name__ == "__main__":
    unittest.main()
