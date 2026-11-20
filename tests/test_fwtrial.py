"""fwtrial 业务规则测试：口径、估计、决策、计划、复核与留痕。"""
from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from fwtrial.contract import TZ, load_contract
from fwtrial.decide import evaluate_requests
from fwtrial.estimate import (build_station_frames, compute_metrics, dedup_checks,
                              station_icc)
from fwtrial.events import eventize, family_events, rename_audit
from fwtrial.exposure import arm_frame, build_exposure
from fwtrial.ledger import Ledger
from fwtrial.looks import adhoc_confidence, registered_looks
from fwtrial.plan import plan_checks
from fwtrial.review import find_reviews
from packhelper import mini_tables, write_pack

CUTOFF = dt.datetime(2025, 1, 5, 8, 0, tzinfo=TZ)


class PackCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = write_pack(Path(self.tmp.name), mini_tables())

    def tearDown(self):
        self.tmp.cleanup()

    def contract(self, cutoff=CUTOFF):
        return load_contract(self.root, cutoff)


class ContractTests(PackCase):
    def test_cutoff_hides_future_facts(self):
        early = dt.datetime(2025, 1, 2, 8, 0, tzinfo=TZ)
        contract = self.contract(early)
        self.assertTrue((contract.alarms["available_at"] <= early).all())
        self.assertLess(len(contract.alarms), len(self.contract().alarms))

    def test_empty_result_is_unknown_not_negative(self):
        tables = mini_tables()
        tables["verifications"].append(
            ["QP1", "EV3-2-0", "E3-2-0", "random", "0.2",
             "2025-01-04T15:00:00+08:00", "", "", "2025-01-05T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "pending", tables)
        contract = load_contract(root, CUTOFF)
        checks = dedup_checks(contract.verifications)
        pending = checks[checks["check_id"] == "QP1"].iloc[0]
        self.assertFalse(bool(pending["executed"]))
        self.assertIsNone(pending["need_action"])
        metrics = compute_metrics(list(build_station_frames(contract, "resident").values()))
        old = metrics["per_version"]["old"]
        # 待核实不算阴性：已执行抽查仍是 2 项（QR4、QR5）
        self.assertEqual(old["pending_checks"], 1)
        self.assertEqual(old["random_noalarm_checks"], 2)
        # 但识别界上界包含这项待核实的权重 1/0.2
        self.assertAlmostEqual(old["missed_bound_hi"] - old["missed_rate"],
                               5.0 / old["exposure_total_h"], places=6)


class EventTests(PackCase):
    def test_duplicates_collapse_to_one_event(self):
        contract = self.contract()
        events = eventize(contract.alarms, contract.resident, contract.code_map)
        ev = events[events["event_id"] == "EV0-1-1"].iloc[0]
        self.assertEqual(ev["n_messages"], 2)
        self.assertEqual(ev["kind"], "duplicated")
        self.assertEqual(len(events), 5)  # 6 条报送 → 5 起事件

    def test_rename_is_not_improvement(self):
        contract = self.contract()
        audit = rename_audit(contract.alarms, contract.resident, contract.code_map)
        families = {r["family"] for r in audit["renamed_families"]}
        self.assertIn("thermal", families)  # A1/T01 是同一语义的改名

    def test_merged_event_expands_by_family(self):
        contract = self.contract()
        alarms = pd.concat([contract.alarms, pd.DataFrame([{
            "message_id": "M1", "event_id": "EVM", "exposure_id": "E0-1-1",
            "code": "A1", "occurred_at": dt.datetime(2025, 1, 1, 11, tzinfo=TZ),
            "available_at": dt.datetime(2025, 1, 2, 8, tzinfo=TZ)}])])
        code_map = pd.concat([contract.code_map, pd.DataFrame([
            {"version": "old", "code": "B1", "family": "voltage"}])])
        alarms.loc[len(alarms)] = ["M2", "EVM", "E0-1-1", "B1",
                                   dt.datetime(2025, 1, 1, 11, 5, tzinfo=TZ),
                                   dt.datetime(2025, 1, 2, 8, tzinfo=TZ)]
        events = eventize(alarms, contract.resident, code_map)
        merged = events[events["event_id"] == "EVM"].iloc[0]
        self.assertEqual(merged["kind"], "merged")
        self.assertEqual(len(family_events(events[events["event_id"] == "EVM"])), 2)


class ExposureTests(PackCase):
    def test_mismatch_day_splits_arms(self):
        exposure = build_exposure(self.contract())
        day = exposure[exposure["exposure_id"] == "E2-2-1"].iloc[0]
        self.assertEqual(day["assigned_version"], "new")
        self.assertEqual(day["resident_version"], "old")  # 升级失败各归其臂

    def test_rollback_quiet_not_new_benefit(self):
        tables = mini_tables()
        # S2G1 第 3 日起回退：之后安排新而实际旧的组日是回退后平静时段
        for row in tables["resident"]:
            if row[0] == "E3-2-1":
                row[1] = "old"
        tables["interventions"].append(
            ["RB1", "E2-2-1", "2025-01-04T00:00:00+08:00", "rollback", "2",
             "2025-01-04T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "rb", tables)
        exposure = build_exposure(load_contract(root, CUTOFF))
        quiet = exposure[exposure["post_rollback_quiet"]]
        self.assertEqual(set(quiet["exposure_id"]), {"E3-2-1"})
        # 干预日（E2-2-1，实际旧版）不计入对应臂的收益暴露
        metrics = compute_metrics(list(build_station_frames(
            load_contract(root, CUTOFF), "resident").values()))
        old = metrics["per_version"]["old"]
        self.assertLess(old["exposure_benefit_h"], old["exposure_total_h"])

    def test_intervention_day_excluded_from_benefit_only(self):
        tables = mini_tables()
        tables["interventions"].append(
            ["IN1", "E1-2-0", "2025-01-02T14:00:00+08:00", "inspection_stop", "2",
             "2025-01-03T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "iv", tables)
        metrics = compute_metrics(list(build_station_frames(
            load_contract(root, CUTOFF), "resident").values()))
        old = metrics["per_version"]["old"]
        self.assertEqual(old["exposure_total_h"] - old["exposure_benefit_h"], 20.0)
        self.assertEqual(old["severe_n"], 1)  # 安全指标保留干预事件

    def test_intervention_day_events_excluded_from_nuisance(self):
        # 干预日分子分母一致剔除：E0-1-1 干预后，其无效告警不计入无效估计
        tables = mini_tables()
        tables["interventions"].append(
            ["IN1", "E0-1-1", "2025-01-01T14:00:00+08:00", "inspection_stop", "2",
             "2025-01-02T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "iv2", tables)
        metrics = compute_metrics(list(build_station_frames(
            load_contract(root, CUTOFF), "resident").values()))
        old = metrics["per_version"]["old"]
        self.assertAlmostEqual(old["invalid_est"], 2.0)  # EV0-1-1 不再计入
        self.assertAlmostEqual(old["exposure_benefit_h"], 160.0)  # 180 - 20（含升级失败日）

    def test_standardize_with_stratum_only_on_intervention_day(self):
        from fwtrial.estimate import standardized_nuisance
        tables = mini_tables()
        for row in tables["resident"]:
            if row[0] == "E0-1-0":
                row[3] = "9.9"  # 该负荷层只出现在干预日
        tables["interventions"].append(
            ["IN1", "E0-1-0", "2025-01-01T14:00:00+08:00", "inspection_stop", "2",
             "2025-01-02T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "strata", tables)
        station_map = build_station_frames(load_contract(root, CUTOFF), "resident")
        result = standardized_nuisance(list(station_map.values()))
        self.assertIn("new", result["standardized_nuisance_rate"])


class EstimateTests(PackCase):
    def test_ipw_weighting(self):
        metrics = compute_metrics(list(build_station_frames(self.contract(), "resident").values()))
        old = metrics["per_version"]["old"]
        # QR2 是旧版无告警日唯一阳性抽查：1/0.2 = 5
        self.assertAlmostEqual(old["missed_est"], 5.0)
        self.assertAlmostEqual(old["missed_rate"], 5.0 / old["exposure_total_h"])

    def test_recheck_does_not_add_sample(self):
        tables = mini_tables()
        tables["verifications"].append(
            ["QEV0-1-1B", "EV0-1-1", "E0-1-1", "targeted", "",
             "2025-01-01T18:00:00+08:00", "false", "30", "2025-01-02T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "recheck", tables)
        metrics = compute_metrics(list(build_station_frames(
            load_contract(root, CUTOFF), "resident").values()))
        old = metrics["per_version"]["old"]
        self.assertEqual(old["rechecks_dropped"], 1)
        self.assertAlmostEqual(old["invalid_est"], 3.0)  # 仍按 3 起无效计

    def test_unverified_alarm_widens_identification_bound(self):
        tables = mini_tables()
        tables["alarms"].append(
            ["EVX-0", "EVX", "E3-2-1", "T01", "2025-01-04T10:00:00+08:00",
             "2025-01-05T08:00:00+08:00"])  # 无核实记录的告警事件
        root = write_pack(Path(self.tmp.name) / "unverified", tables)
        metrics = compute_metrics(list(build_station_frames(
            load_contract(root, CUTOFF), "resident").values()))
        new = metrics["per_version"]["new"]
        self.assertEqual(new["unverified_alarm_events"], 1)
        self.assertGreater(new["nuisance_bound_hi"], new["nuisance_bound_lo"])

    def test_cluster_structure(self):
        station_map = build_station_frames(self.contract(), "resident")
        icc = station_icc(station_map)
        self.assertEqual(icc["clusters"], 2)
        self.assertGreaterEqual(icc["deff"], 1.0)


class DecisionTests(PackCase):
    def decide(self, tables=None, B=100):
        root = self.root if tables is None else write_pack(
            Path(self.tmp.name) / "case", tables)
        ledger = Ledger(Path(self.tmp.name) / "ledger.jsonl")
        return evaluate_requests(root, ledger, B=B), ledger

    def test_capacity_limited_expansion_keeps_all_high_risk(self):
        tables = mini_tables()
        tables["stations"][0][2] = "1"  # S1 每日检查名额压到 1，容量必然约束
        decisions, _ = self.decide(tables)
        self.assertEqual(len(decisions), 1)
        d = decisions[0]
        self.assertEqual(d["decision"], "expand_limited")  # 容量约束降规模
        self.assertLess(d["recommended_units"], d["requested_units"])
        self.assertEqual(d["high_risk_events_excluded"], 0)  # 护栏
        self.assertGreater(d["acted_events_counted"], 0)

    def test_pause_when_safety_bound_exceeded(self):
        tables = mini_tables()
        # 新版无告警日两起抽查阳性 → 漏处置风险上界越限
        tables["verifications"] += [
            ["QR8", "EV3-1-0", "E3-1-0", "random", "0.2", "2025-01-04T15:00:00+08:00",
             "true", "45", "2025-01-05T08:00:00+08:00"],
            ["QR9", "EV3-2-1B", "E3-2-1", "random", "0.2", "2025-01-04T16:00:00+08:00",
             "true", "45", "2025-01-05T08:00:00+08:00"],
        ]
        tables["rollout_requests"][0][3] = "0.01"  # 允许增量收紧
        decisions, _ = self.decide(tables)
        self.assertEqual(decisions[0]["decision"], "pause")

    def test_decision_provenance_recorded(self):
        _, ledger = self.decide()
        record = ledger.of_kind("decision")[0]
        self.assertEqual(record["cutoff"], "2025-01-05T08:00:00+08:00")
        self.assertTrue(record["code_map_digest"])
        self.assertTrue(record["fact_digest"])
        self.assertEqual(record["epoch"], 1)

    def test_pause_recommends_zero_units(self):
        tables = mini_tables()
        tables["verifications"] += [
            ["QR8", "EV3-1-0", "E3-1-0", "random", "0.2", "2025-01-04T15:00:00+08:00",
             "true", "45", "2025-01-05T08:00:00+08:00"],
            ["QR9", "EV3-2-1B", "E3-2-1", "random", "0.2", "2025-01-04T16:00:00+08:00",
             "true", "45", "2025-01-05T08:00:00+08:00"],
        ]
        tables["rollout_requests"][0][3] = "0.01"
        decisions, _ = self.decide(tables)
        self.assertEqual(decisions[0]["decision"], "pause")
        self.assertEqual(decisions[0]["recommended_units"], 0)  # 暂停不建议规模

    def test_bonferroni_widens_within_batch(self):
        tables = mini_tables()
        tables["rollout_requests"].append(
            ["R2", "S2", "4", "0.05", "0.10", "2025-01-05T08:00:00+08:00", "2025-01-10"])
        decisions, _ = self.decide(tables)
        self.assertEqual(len(decisions), 2)
        # 同批后续决定按更多已发生查看调整
        self.assertGreater(decisions[1]["looks_adjusted_for"],
                           decisions[0]["looks_adjusted_for"])
        self.assertGreater(decisions[1]["confidence_level"],
                           decisions[0]["confidence_level"])


class LookTests(PackCase):
    def test_registered_looks_spend_alpha(self):
        contract = self.contract()
        ledger = Ledger(Path(self.tmp.name) / "ledger.jsonl")
        results = registered_looks(self.root, contract.review_policy, ledger=ledger, B=50)
        self.assertEqual(len(results), 2)
        spent = sum(r["alpha_increment"] for r in results)
        self.assertLessEqual(spent, 0.05 + 1e-9)
        # 查看已留痕，随后的临时查看区间必须加宽
        level, n = adhoc_confidence(ledger)
        self.assertGreater(n, 1)
        self.assertGreater(level, 0.95)


class PlanTests(PackCase):
    def test_budget_and_random_reserve(self):
        tables = mini_tables()
        # 一起未核实告警形成定向需求；S2 预算压到 1
        tables["alarms"].append(
            ["EVY-0", "EVY", "E3-2-1", "T01", "2025-01-04T10:00:00+08:00",
             "2025-01-05T08:00:00+08:00"])
        tables["stations"][1][2] = "1"
        root = write_pack(Path(self.tmp.name) / "plan", tables)
        plan = plan_checks(root, dt.date(2025, 1, 5))
        tasks = plan["tasks"]
        for station, sub in tasks.groupby("station_id"):
            self.assertLessEqual(len(sub), 3)
        s2 = tasks[tasks["station_id"] == "S2"]
        self.assertLessEqual(len(s2), 1)
        # 预算再紧也要保抽查下限
        self.assertEqual(set(s2["kind"]), {"random"})

    def test_pending_check_day_not_replanned(self):
        tables = mini_tables()
        # E3-2-0 已有待执行抽查：次日计划不得重复安排同一组日
        tables["verifications"].append(
            ["QP1", "EV3-2-0", "E3-2-0", "random", "0.2", "2025-01-04T15:00:00+08:00",
             "", "", "2025-01-04T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "noplan", tables)
        plan = plan_checks(root, dt.date(2025, 1, 5))
        random_days = plan["tasks"][plan["tasks"]["kind"] == "random"]["exposure_id"]
        self.assertNotIn("E3-2-0", set(random_days))


class ReviewTests(PackCase):
    def test_late_positive_triggers_review(self):
        tables = mini_tables()
        # S1 的阳性核实在决定截止后才可知
        tables["verifications"].append(
            ["QR9", "EV3-1-0", "E3-1-0", "random", "0.2", "2025-01-04T15:00:00+08:00",
             "true", "50", "2025-01-05T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "late", tables)
        ledger = Ledger(Path(self.tmp.name) / "ledger.jsonl")
        ledger.append("decision", {
            "request_id": "R1", "station_id": "S1", "decision": "expand",
            "cutoff": "2025-01-03T08:00:00+08:00", "epoch": 1})
        result = find_reviews(root, ledger)
        self.assertEqual(result["decisions_checked"], 1)
        self.assertEqual(result["reviews"][0]["status"], "需要复核")
        self.assertEqual(result["reviews"][0]["request_id"], "R1")

    def test_no_decisions_no_reviews(self):
        ledger = Ledger(Path(self.tmp.name) / "ledger.jsonl")
        result = find_reviews(self.root, ledger)
        self.assertEqual(result["decisions_checked"], 0)

    def test_pending_at_cutoff_flags_watch_item(self):
        tables = mini_tables()
        # 截止前已登记但始终未执行的核实
        tables["verifications"].append(
            ["QP1", "EV3-1-0", "E3-1-0", "random", "0.2", "2025-01-04T15:00:00+08:00",
             "", "", "2025-01-04T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "watch", tables)
        ledger = Ledger(Path(self.tmp.name) / "ledger.jsonl")
        ledger.append("decision", {
            "request_id": "R1", "station_id": "S1", "decision": "expand",
            "cutoff": "2025-01-05T08:00:00+08:00", "epoch": 1})
        result = find_reviews(root, ledger)
        self.assertEqual(result["reviews"][0]["status"], "待复核观察项")

    def test_review_records_not_duplicated(self):
        tables = mini_tables()
        tables["verifications"].append(
            ["QR9", "EV3-1-0", "E3-1-0", "random", "0.2", "2025-01-04T15:00:00+08:00",
             "true", "50", "2025-01-05T08:00:00+08:00"])
        root = write_pack(Path(self.tmp.name) / "dup", tables)
        ledger = Ledger(Path(self.tmp.name) / "ledger.jsonl")
        ledger.append("decision", {
            "request_id": "R1", "station_id": "S1", "decision": "expand",
            "cutoff": "2025-01-03T08:00:00+08:00", "epoch": 1})
        find_reviews(root, ledger)
        find_reviews(root, ledger)  # 重复运行不得重复留痕
        self.assertEqual(len(ledger.of_kind("review")), 1)


class LedgerTests(unittest.TestCase):
    def test_epoch_changes_with_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.jsonl")
            self.assertEqual(ledger.current_epoch("A"), 1)
            ledger.append("decision", {"request_id": "R", "epoch": 1, "strategy_key": "A",
                                       "decision": "expand"})
            self.assertEqual(ledger.current_epoch("A"), 1)
            self.assertEqual(ledger.current_epoch("B"), 2)  # 策略变化开启新纪元
            ledger.append("decision", {"request_id": "R2", "epoch": 2, "strategy_key": "B",
                                       "decision": "pause"})
            self.assertEqual(ledger.current_epoch("A"), 1)  # 回到旧策略仍属旧纪元


if __name__ == "__main__":
    unittest.main()
