"""有效暴露与试验口径。

- ITT（计划口径）：按 assignment 的安排版本分组，衡量"安排新固件"这一策略；
- as-treated（实际口径）：按 resident 的实际驻留版本分组，衡量固件本身。
- 严重事件干预/回退所在组日不计入收益类暴露（回退后的平静时段不能
  继续算作新固件的安全收益），但该日及其事件仍计入安全类指标。
- 回退后 assigned=new 而 resident=old 的组日是"回退后平静时段"：
  在实际口径中属于旧版暴露；在计划口径中属于新臂的策略成本；
  任何口径都不得记为新固件收益。
"""
from __future__ import annotations

import pandas as pd

BENEFIT_METRICS = ("nuisance",)                    # 收益类：干预日整体剔除
SAFETY_METRICS = ("missed", "severe", "delay")     # 安全类：干预日保留


def build_exposure(contract) -> pd.DataFrame:
    """组日暴露表：计划与实际版本并列，标注干预日与回退后平静时段。"""
    frame = contract.assignment.merge(contract.resident, on="exposure_id", how="inner")
    if len(frame) != len(contract.assignment):
        missing = set(contract.assignment["exposure_id"]) - set(contract.resident["exposure_id"])
        raise ValueError("组日缺少驻留记录：" + ",".join(sorted(missing)))

    flagged = set(contract.interventions["exposure_id"])
    frame["intervention"] = frame["exposure_id"].isin(flagged)
    kinds = contract.interventions.set_index("exposure_id")["kind"]
    frame["intervention_kind"] = frame["exposure_id"].map(kinds).fillna("")

    # 回退后平静时段：同站同组在回退之后、安排仍为新而实际为旧的组日
    frame = frame.sort_values(["station_id", "group_id", "date"]).reset_index(drop=True)
    frame["post_rollback_quiet"] = False
    rollbacks = contract.interventions[contract.interventions["kind"] == "rollback"]
    for rb in rollbacks.itertuples():
        anchor = frame[frame["exposure_id"] == rb.exposure_id]
        if anchor.empty:
            continue
        key = anchor.iloc[0]
        mask = (
            (frame["station_id"] == key["station_id"])
            & (frame["group_id"] == key["group_id"])
            & (frame["date"] > key["date"])
            & (frame["assigned_version"] == "new")
            & (frame["resident_version"] == "old")
        )
        frame.loc[mask, "post_rollback_quiet"] = True
    return frame


def arm_frame(exposure: pd.DataFrame, arm: str) -> pd.DataFrame:
    """按口径给出每组日的臂别。arm: 'assigned'（ITT）或 'resident'（实际）。"""
    if arm not in ("assigned", "resident"):
        raise ValueError("未知口径：" + arm)
    frame = exposure.copy()
    frame["arm_version"] = frame["assigned_version"] if arm == "assigned" else frame["resident_version"]
    return frame


def exposure_summary(exposure: pd.DataFrame) -> dict:
    """可比性事实：暴露量、失衡、干预日、回退后平静时段。"""
    by_arm = {}
    for arm in ("assigned", "resident"):
        frame = arm_frame(exposure, arm)
        hours = frame.groupby("arm_version")["run_hours"].sum().to_dict()
        by_arm[arm] = {k: round(v, 3) for k, v in hours.items()}
    mismatch = exposure[exposure["assigned_version"] != exposure["resident_version"]]
    quiet = exposure[exposure["post_rollback_quiet"]]
    return {
        "hours_by_arm": by_arm,
        "mismatch_days": int(len(mismatch)),
        "mismatch_by_station": mismatch.groupby("station_id").size().to_dict(),
        "intervention_days": int(exposure["intervention"].sum()),
        "post_rollback_quiet_days": int(len(quiet)),
        "post_rollback_quiet_hours": round(float(quiet["run_hours"].sum()), 3),
    }
