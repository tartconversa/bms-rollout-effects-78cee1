"""效果估计：单位有效运行暴露下的无效打扰、漏处置风险与处置延迟。

原则：
- 核实选择差异进入安全解释：定向核实 π=1，登记抽查 π=登记概率，
  估计用 Horvitz–Thompson 逆概率加权。
- 待核实与未检查不算阴性：它们不进入分子分母，而是形成识别界
  （最好/最坏情形），宽度即不可识别部分。
- 未执行的计划不进入效果分母；同一事件的重复核查不增加样本量。
- 站内关联：不确定性用站级整群 bootstrap，并报告 ICC 与设计效应。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .events import eventize
from .exposure import arm_frame, build_exposure

VERSIONS = ("old", "new")


@dataclass
class Frames:
    """一个站在一个口径下的全部分析素材。"""

    station_id: str
    exposure: pd.DataFrame      # 含 arm_version, run_hours, intervention, load_index
    events: pd.DataFrame        # 告警事件（含 n_messages, families）
    checks: pd.DataFrame        # 去重后的核实（含 pi, executed, need_action）
    interventions: pd.DataFrame


def dedup_checks(verifications: pd.DataFrame) -> pd.DataFrame:
    """核实去重与口径整理。

    - executed = need_action 已知；待核实（空）不算阴性也不进分母。
    - 同一 event_id 的多次已执行核查只保留最早一次，其余记为重复核查，
      不增加样本量。
    - 定向 π=1；登记抽查 π=probability（必须在 (0,1]）。
    """
    frame = verifications.copy()
    frame["executed"] = frame["need_action"].notna()
    frame["pi"] = [
        1.0 if sel == "targeted" else (prob if prob and 0 < prob <= 1 else np.nan)
        for sel, prob in zip(frame["selection"], frame["probability"])
    ]
    if frame.loc[frame["selection"] == "random", "pi"].isna().any():
        raise ValueError("登记抽查缺少有效入选概率")
    frame = frame.sort_values(["event_id", "checked_at", "check_id"]).reset_index(drop=True)
    frame["recheck"] = False
    executed = frame["executed"]
    first = frame[executed].groupby("event_id").head(1).index
    dup = frame[executed & ~frame.index.isin(first) & frame["event_id"].isin(frame.loc[first, "event_id"])]
    frame.loc[dup.index, "recheck"] = True
    return frame


def build_station_frames(contract, arm: str) -> dict:
    """按站切分素材；站内组共享班组，站是独立性的基本单位。"""
    exposure = arm_frame(build_exposure(contract), arm)
    events = eventize(contract.alarms, contract.resident, contract.code_map)
    checks = dedup_checks(contract.verifications)

    key = exposure.set_index("exposure_id")

    def _place(frame):
        return frame.assign(
            station_id=frame["exposure_id"].map(key["station_id"]),
            arm_version=frame["exposure_id"].map(key["arm_version"]),
        )

    events = _place(events)
    checks = _place(checks)
    interventions = _place(contract.interventions.copy())

    alarm_event_ids = set(events["event_id"])
    checks["has_alarm"] = checks["event_id"].isin(alarm_event_ids)

    out = {}
    for station_id, exp in exposure.groupby("station_id"):
        out[station_id] = Frames(
            station_id=station_id,
            exposure=exp,
            events=events[events["station_id"] == station_id],
            checks=checks[checks["station_id"] == station_id],
            interventions=interventions[interventions["station_id"] == station_id],
        )
    return out


def _combine(stations: list) -> dict:
    return {
        "exposure": pd.concat([s.exposure for s in stations]),
        "events": pd.concat([s.events for s in stations]),
        "checks": pd.concat([s.checks for s in stations]),
        "interventions": pd.concat([s.interventions for s in stations]),
    }


def _version_metrics(data: dict, version: str) -> dict:
    exp = data["exposure"][data["exposure"]["arm_version"] == version]
    ev = data["events"][data["events"]["arm_version"] == version]
    chk = data["checks"][data["checks"]["arm_version"] == version]
    itv = data["interventions"][data["interventions"]["arm_version"] == version]

    benefit_exp = exp[~exp["intervention"]]
    benefit_ids = set(benefit_exp["exposure_id"])
    exposure_benefit_h = float(benefit_exp["run_hours"].sum())
    exposure_total_h = float(exp["run_hours"].sum())

    done = chk[chk["executed"] & ~chk["recheck"]]
    pending = chk[~chk["executed"]]

    # 无效打扰：告警事件经核实不需要处置。干预日整体剔除（分子分母一致），
    # 待核实/未检查不算阴性，进识别界。
    alarm_done = done[done["has_alarm"] & done["exposure_id"].isin(benefit_ids)]
    invalid_est = float((~alarm_done["need_action"].astype(bool) / alarm_done["pi"]).sum()) if len(alarm_done) else 0.0
    verified_alarm_ids = set(done["event_id"])
    ev_benefit = ev[ev["exposure_id"].isin(benefit_ids)]
    unverified_alarm_events = int((~ev_benefit["event_id"].isin(verified_alarm_ids)).sum())
    pending_alarm = pending[pending["has_alarm"] & pending["exposure_id"].isin(benefit_ids)]
    upper_extra = float((1.0 / pending_alarm["pi"]).sum()) + max(0, unverified_alarm_events - len(pending_alarm))
    nuisance_rate = invalid_est / exposure_benefit_h if exposure_benefit_h else np.nan

    # 漏处置风险：无告警组日的登记抽查发现需要处置。只有 random 抽查能见到无告警日。
    random_noalarm = done[(done["selection"] == "random") & ~done["has_alarm"]]
    missed_est = float((random_noalarm["need_action"].astype(bool) / random_noalarm["pi"]).sum()) if len(random_noalarm) else 0.0
    pending_noalarm = pending[(pending["selection"] == "random") & ~pending["has_alarm"]]
    missed_upper_extra = float((1.0 / pending_noalarm["pi"]).sum()) if len(pending_noalarm) else 0.0
    missed_rate = missed_est / exposure_total_h if exposure_total_h else np.nan

    # 处置延迟：经核实需要处置的事件。
    acted = done[done["need_action"] == True]  # noqa: E712
    delays = acted["delay_minutes"].dropna()

    severe_n = int(len(itv))
    return {
        "exposure_benefit_h": exposure_benefit_h,
        "exposure_total_h": exposure_total_h,
        "alarm_events": int(len(ev)),
        "duplicated_messages": int((ev["n_messages"] - 1).sum()) if len(ev) else 0,
        "merged_events": int((ev["n_family"] > 1).sum()) if len(ev) else 0,
        "invalid_est": invalid_est,
        "nuisance_rate": nuisance_rate,
        "nuisance_bound_lo": invalid_est / exposure_benefit_h if exposure_benefit_h else np.nan,
        "nuisance_bound_hi": (invalid_est + upper_extra) / exposure_benefit_h if exposure_benefit_h else np.nan,
        "unverified_alarm_events": unverified_alarm_events,
        "missed_est": missed_est,
        "missed_rate": missed_rate,
        "missed_bound_hi": (missed_est + missed_upper_extra) / exposure_total_h if exposure_total_h else np.nan,
        "random_noalarm_checks": int(len(random_noalarm)),
        "pending_checks": int(len(pending)),
        "rechecks_dropped": int(chk["recheck"].sum()),
        "acted_n": int(len(acted)),
        "delay_median": float(delays.median()) if len(delays) else np.nan,
        "delay_mean": float(delays.mean()) if len(delays) else np.nan,
        "delay_n": int(len(delays)),
        "severe_n": severe_n,
        "severe_rate": severe_n / exposure_total_h if exposure_total_h else np.nan,
    }


def compute_metrics(stations: list) -> dict:
    """合并若干站，给出两版指标与差值（new − old）。"""
    data = _combine(stations)
    per_version = {v: _version_metrics(data, v) for v in VERSIONS}
    new, old = per_version["new"], per_version["old"]
    diffs = {
        "nuisance_rate_diff": new["nuisance_rate"] - old["nuisance_rate"],
        "nuisance_reduction_rel": (1 - new["nuisance_rate"] / old["nuisance_rate"])
        if old["nuisance_rate"] else np.nan,
        "missed_rate_diff": new["missed_rate"] - old["missed_rate"],
        "severe_rate_diff": new["severe_rate"] - old["severe_rate"],
        "delay_median_diff": new["delay_median"] - old["delay_median"],
    }
    return {"per_version": per_version, "diffs": diffs}


def standardized_nuisance(stations: list) -> dict:
    """按负荷指数分层直接标准化：负荷结构差异不得解释成固件效果。"""
    data = _combine(stations)
    exp = data["exposure"]
    chk = data["checks"]
    benefit = exp[~exp["intervention"]]
    strata = sorted(benefit["load_index"].unique())  # 分层只取收益暴露中出现的负荷
    weights = benefit.groupby("load_index")["run_hours"].sum()
    weights = weights / weights.sum()
    out = {}
    notes = []
    for version in VERSIONS:
        rate = 0.0
        for s in strata:
            exp_vs = benefit[(benefit["arm_version"] == version) & (benefit["load_index"] == s)]
            hours = float(exp_vs["run_hours"].sum())
            if hours <= 0:
                notes.append(f"{version} 在负荷层 {s} 无暴露，该层按粗率补充")
                rate += float(weights[s]) * _version_metrics(
                    {"exposure": exp, "events": data["events"], "checks": chk,
                     "interventions": data["interventions"]}, version)["nuisance_rate"]
                continue
            ids = set(exp_vs["exposure_id"])
            layer = chk[chk["executed"] & ~chk["recheck"] & chk["has_alarm"] & chk["exposure_id"].isin(ids)]
            invalid = float((~layer["need_action"].astype(bool) / layer["pi"]).sum()) if len(layer) else 0.0
            rate += float(weights[s]) * invalid / hours
        out[version] = rate
    return {"standardized_nuisance_rate": out, "strata": [float(s) for s in strata], "notes": notes}


def station_icc(stations) -> dict:
    """组日告警事件数的站级 ICC 与设计效应（单因素随机效应近似）。"""
    if isinstance(stations, dict):
        stations = list(stations.values())
    rows = []
    for s in stations:
        exp = s.exposure
        counts = s.events.groupby("exposure_id").size()
        day_counts = exp["exposure_id"].map(counts).fillna(0.0)
        rows.append(pd.DataFrame({"station": s.station_id, "count": day_counts.values}))
    frame = pd.concat(rows)
    k = frame["station"].nunique()
    n = frame.groupby("station").size()
    if k < 2:
        return {"icc": np.nan, "deff": np.nan, "effective_n": np.nan, "clusters": k}
    grand = frame["count"].mean()
    msb = float((n * (frame.groupby("station")["count"].mean() - grand) ** 2).sum() / (k - 1))
    within = frame.groupby("station")["count"].apply(lambda s: ((s - s.mean()) ** 2).sum())
    msw = float(within.sum() / (len(frame) - k))
    m0 = float((n.sum() - (n**2).sum() / n.sum()) / (k - 1))
    icc = max(0.0, (msb - msw) / (msb + (m0 - 1) * msw)) if msb + (m0 - 1) * msw > 0 else 0.0
    deff = 1 + (m0 - 1) * icc
    return {"icc": round(icc, 4), "deff": round(deff, 3),
            "effective_n": round(float(n.sum()) / deff, 1), "clusters": int(k)}


def cluster_bootstrap(station_map: dict, B: int = 1000, seed: int = 20251120,
                      level: float = 0.95) -> dict:
    """站级整群 bootstrap：站内组日相关，重抽样必须以站为单位。"""
    names = sorted(station_map)
    rng = np.random.default_rng(seed)
    keys = ("nuisance_rate_diff", "nuisance_reduction_rel", "missed_rate_diff",
            "severe_rate_diff", "delay_median_diff")
    draws = {k: [] for k in keys}
    for _ in range(B):
        picked = [station_map[names[i]] for i in rng.integers(0, len(names), len(names))]
        diffs = compute_metrics(picked)["diffs"]
        for k in keys:
            draws[k].append(diffs[k])
    alpha = 1 - level
    intervals = {}
    for k, values in draws.items():
        arr = np.asarray([v for v in values if np.isfinite(v)], dtype=float)
        if len(arr) < B * 0.5:
            intervals[k] = (np.nan, np.nan)
            continue
        lo, hi = np.percentile(arr, [100 * alpha / 2, 100 * (1 - alpha / 2)])
        intervals[k] = (float(lo), float(hi))
    return {"level": level, "B": B, "intervals": intervals}


def covariate_balance(station_map: dict) -> pd.DataFrame:
    """负荷与设备年龄在臂间的均衡性（交叉随机化应使其接近）。"""
    data = _combine(list(station_map.values()))
    exp = data["exposure"]
    return exp.groupby("arm_version")[["load_index", "age_days"]].mean().round(3)
