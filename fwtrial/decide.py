"""扩围决策：试验支持程度与扩围后工作量的联合判断。

决定分四档：expand（支持扩围）、expand_limited（限定站群/规模）、
continue_trial（证据不足，继续试用）、pause（安全上界越限，暂停）。

护栏：容量只影响可支持的规模与核实安排，绝不进入安全估计；
高风险（需处置）事件永远计入指标，不能因为现场忙不过来而被排除
后再宣称达标。
"""
from __future__ import annotations

import numpy as np

from .contract import load_contract
from .estimate import build_station_frames, cluster_bootstrap, compute_metrics
from .ledger import strategy_key
from .looks import adhoc_confidence
from .params import MIN_RANDOM_CHECKS, RANDOM_PI


def _project_workload(station_row, per_version_new: dict, units: int) -> dict:
    """按试用中新版的单位强度外推扩围后的每日核实与处置工作量。

    处置分钟以核实需处置事件的平均处置延迟作代理（资料未含处置工时，
    该代理在报告中明示）。
    """
    exposure_days = max(per_version_new["exposure_total_h"] / 24.0, 1e-9)
    alarm_per_unit_day = per_version_new["alarm_events"] / exposure_days
    acted_per_unit_day = per_version_new["acted_n"] / exposure_days
    checks_targeted = alarm_per_unit_day * units
    checks_random = RANDOM_PI * max(1 - alarm_per_unit_day, 0.0) * units
    delay_mean = per_version_new["delay_mean"] if np.isfinite(per_version_new["delay_mean"]) else 60.0
    action_minutes = acted_per_unit_day * units * delay_mean
    return {
        "checks_targeted_daily": round(checks_targeted, 2),
        "checks_random_daily": round(checks_random, 2),
        "checks_total_daily": round(checks_targeted + checks_random, 2),
        "action_minutes_daily": round(action_minutes, 1),
        "capacity_checks": int(station_row["checks_daily"]),
        "capacity_action_minutes": int(station_row["action_minutes_daily"]),
    }


def _max_units_by_capacity(workload_per_unit: dict, station_row) -> int:
    limits = []
    if workload_per_unit["checks_total_daily"] > 0:
        limits.append(int(station_row["checks_daily"] // workload_per_unit["checks_total_daily"]))
    if workload_per_unit["action_minutes_daily"] > 0:
        limits.append(int(station_row["action_minutes_daily"]
                          // workload_per_unit["action_minutes_daily"]))
    return min(limits) if limits else 10 ** 6


def evaluate_requests(root, ledger, B: int = 500) -> list:
    """对全部可见扩围申请逐一评估并留痕。返回决定记录列表。"""
    requests_contract = load_contract(root)  # 仅为读取申请与静态资料
    requests = list(requests_contract.rollout_requests.itertuples())
    if not requests:
        return []
    key = strategy_key(requests)
    epoch = ledger.current_epoch(key)

    decisions = []
    for req in requests:
        # 每份申请都是一次查看：置信水平随台账中已发生查看次数逐个加宽
        level, n_looks = adhoc_confidence(ledger)
        contract = load_contract(root, req.cutoff)
        station_map = build_station_frames(contract, arm="resident")
        itt_map = build_station_frames(contract, arm="assigned")
        metrics = compute_metrics(list(station_map.values()))
        itt_metrics = compute_metrics(list(itt_map.values()))
        boot = cluster_bootstrap(station_map, B=B, level=level)
        new = metrics["per_version"]["new"]
        old = metrics["per_version"]["old"]
        diffs = metrics["diffs"]

        station_row = contract.stations.set_index("station_id").loc[req.station_id]
        workload = _project_workload(station_row, new, req.units)
        per_unit = _project_workload(station_row, new, 1)
        max_units = _max_units_by_capacity(per_unit, station_row)

        missed_hi = boot["intervals"]["missed_rate_diff"][1]
        severe_hi = boot["intervals"]["severe_rate_diff"][1]
        reduction = diffs["nuisance_reduction_rel"]
        reduction_lo = boot["intervals"]["nuisance_reduction_rel"][0]

        enough_random = (new["random_noalarm_checks"] + old["random_noalarm_checks"]) >= MIN_RANDOM_CHECKS
        estimable = np.isfinite(missed_hi) and np.isfinite(severe_hi) and np.isfinite(reduction)
        safety_breach = (np.isfinite(missed_hi) and missed_hi > req.safety_increment) or \
                        (np.isfinite(severe_hi) and severe_hi > req.safety_increment)
        benefit_ok = np.isfinite(reduction) and reduction >= req.nuisance_reduction
        capacity_ok = (workload["checks_total_daily"] <= workload["capacity_checks"]
                       and workload["action_minutes_daily"] <= workload["capacity_action_minutes"])

        reasons = []
        if not estimable:
            reasons.append("主要指标在当前截止下不可估（暴露或核实不足）")
        if not enough_random:
            reasons.append(f"已执行无告警抽查不足（{new['random_noalarm_checks'] + old['random_noalarm_checks']}<{MIN_RANDOM_CHECKS}），漏处置风险不可识别")
        if np.isfinite(missed_hi) and missed_hi > req.safety_increment:
            reasons.append(f"漏处置风险差上界 {missed_hi:.4f} 超过允许增量 {req.safety_increment}")
        if np.isfinite(severe_hi) and severe_hi > req.safety_increment:
            reasons.append(f"严重事件率差上界 {severe_hi:.4f} 超过允许增量 {req.safety_increment}")
        if not benefit_ok:
            reasons.append(f"打扰相对减少 {reduction:.1%} 未达目标 {req.nuisance_reduction:.1%}（区间下界 {reduction_lo:.1%}）")
        if not capacity_ok:
            reasons.append(f"预计每日核实 {workload['checks_total_daily']} 次 / 处置 "
                           f"{workload['action_minutes_daily']} 分钟，超过容量 "
                           f"{workload['capacity_checks']} 次 / {workload['capacity_action_minutes']} 分钟，"
                           f"可支持约 {max_units} 台")
        if new["pending_checks"] or old["pending_checks"]:
            reasons.append(f"截止时仍有 {new['pending_checks'] + old['pending_checks']} 项核实待执行，未计入任何结论")

        # 站别事实：本站的特殊经历进入解释，但不改写合并估计
        st = station_map[req.station_id]
        st_random = int(((st.checks["selection"] == "random") & st.checks["executed"]
                         & ~st.checks["has_alarm"]).sum())
        st_quiet = int(st.exposure["post_rollback_quiet"].sum())
        st_interventions = int(st.exposure["intervention"].sum())
        if st_quiet:
            reasons.append(f"本站发生回退，回退后平静时段 {st_quiet} 天已排除在新版收益之外；"
                           f"扩围须保留回退预案")
        if st_interventions:
            reasons.append(f"本站试用期干预 {st_interventions} 天，其严重事件已计入安全指标")
        if st_random < 2:
            reasons.append(f"本站已执行无告警抽查仅 {st_random} 项，站别漏处置证据薄弱，"
                           f"结论依赖合并估计")

        if safety_breach:
            decision = "pause"           # 安全上界越限：暂停
        elif not estimable or not enough_random or not benefit_ok:
            decision = "continue_trial"  # 证据不足：继续试用
        elif not capacity_ok:
            decision = "expand_limited"  # 证据支持但容量不足：限定规模
        else:
            decision = "expand"

        # 护栏：高风险事件无一被排除；容量只限制规模，不修改安全估计
        acted_total = int(new["acted_n"] + old["acted_n"])
        record = {
            "type": "adhoc",
            "request_id": req.request_id,
            "station_id": req.station_id,
            "epoch": epoch,
            "strategy_key": key,
            "cutoff": req.cutoff.isoformat(),
            "confidence_level": round(level, 4),
            "looks_adjusted_for": n_looks,
            "code_map_digest": contract.digests["code_map"],
            "fact_digest": contract.fact_digest(),
            "metrics": {"as_treated": metrics, "itt_diffs": itt_metrics["diffs"]},
            "intervals": {k: list(v) for k, v in boot["intervals"].items()},
            "workload": workload,
            "max_units_by_capacity": max_units,
            "requested_units": int(req.units),
            "decision": decision,
            "recommended_units": (int(req.units) if decision == "expand"
                                  else min(int(req.units), max_units) if decision == "expand_limited"
                                  else 0),
            "reasons": reasons,
            "high_risk_events_excluded": 0,
            "acted_events_counted": acted_total,
        }
        decisions.append(record)
        ledger.append("decision", {**record, "metrics": _compact_metrics(record["metrics"])})
    return decisions


def _compact_metrics(metrics: dict) -> dict:
    """台账中只留关键数字，完整结果由分析报告给出。"""
    at = metrics["as_treated"]
    return {
        "diffs": {k: _round(v) for k, v in at["diffs"].items()},
        "new": {k: _round(at["per_version"]["new"][k]) for k in
                ("nuisance_rate", "missed_rate", "severe_rate", "delay_median")},
        "old": {k: _round(at["per_version"]["old"][k]) for k in
                ("nuisance_rate", "missed_rate", "severe_rate", "delay_median")},
        "itt_diffs": {k: _round(v) for k, v in metrics["itt_diffs"].items()},
    }


def _round(value):
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None
