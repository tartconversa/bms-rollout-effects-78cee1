"""核实安排：受检查预算约束的次日核实计划。

- 定向部分：已发生但尚未执行核实的告警事件，按风险优先；
- 抽查部分：按登记概率从无告警组日抽取，保留足以判断无告警群体的
  样本量；预算再紧也不得把抽查挤到零，否则漏处置风险不可识别；
- 计划只是计划：未执行的计划不进入效果分母；执行结果回流后按
  登记概率加权；同一事件重复核查不增加样本量。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from .contract import TZ, load_contract
from .estimate import dedup_checks
from .events import eventize
from .exposure import build_exposure
from .params import MIN_RANDOM_SHARE, RANDOM_PI


def plan_checks(root, plan_date: dt.date, ledger=None, seed: int = 20251120) -> dict:
    """生成 plan_date 当日各站核实任务清单（值班人员视角）。"""
    cutoff = dt.datetime.combine(plan_date, dt.time(8, 0), tzinfo=TZ)
    contract = load_contract(root, cutoff)
    exposure = build_exposure(contract)
    events = eventize(contract.alarms, contract.resident, contract.code_map)
    checks = dedup_checks(contract.verifications)

    done_ids = set(checks.loc[checks["executed"], "event_id"])
    planned_ids = set(checks.loc[~checks["executed"], "event_id"])

    # 定向队列：已知告警事件中尚无已执行核实的（含已计划未执行的，仍欠）
    key = exposure.set_index("exposure_id")
    alarm_pending = events[~events["event_id"].isin(done_ids)].copy()
    alarm_pending["station_id"] = alarm_pending["exposure_id"].map(key["station_id"])
    alarm_pending["already_planned"] = alarm_pending["event_id"].isin(planned_ids)
    alarm_pending["priority"] = np.where(alarm_pending["n_family"] > 1, 0, 1)  # 合并告警优先

    rng = np.random.default_rng(seed)
    stations = contract.stations.set_index("station_id")
    tasks = []
    notes = []
    for station_id in sorted(exposure["station_id"].unique()):
        budget = int(stations.loc[station_id, "checks_daily"])
        targeted = alarm_pending[alarm_pending["station_id"] == station_id].sort_values(
            ["priority", "first_at"])
        n_targeted = min(len(targeted), budget)
        random_budget = budget - n_targeted
        min_random = max(1, int(np.ceil(MIN_RANDOM_SHARE * budget)))
        if random_budget < min_random and len(targeted) > 0:
            # 预算不足时仍保抽查下限，定向部分让位并上报矛盾
            n_targeted = max(0, budget - min_random)
            random_budget = budget - n_targeted
            notes.append(f"{station_id}：预算紧张，定向核实让位于抽查下限 {min_random} 项，"
                         f"其余定向任务顺延")
        for row in targeted.head(n_targeted).itertuples():
            tasks.append({
                "station_id": station_id, "kind": "targeted", "event_id": row.event_id,
                "exposure_id": row.exposure_id, "pi": 1.0,
                "note": "已计划未执行" if row.already_planned else "新增定向",
            })

        # 抽查池：该站最近 3 个可知日、尚未核查也无待执行核查的无告警组日；
        # 按登记概率逐日抽取（实现登记概率，而非任意凑数），再受预算截断
        recent_dates = sorted(exposure[exposure["station_id"] == station_id]["date"].unique())[-3:]
        checked_days = set(checks["exposure_id"])  # 已执行与已计划未执行都不重复安排
        pool = exposure[
            (exposure["station_id"] == station_id)
            & (exposure["date"].isin(recent_dates))
            & ~exposure["exposure_id"].isin(set(events["exposure_id"]))
            & ~exposure["exposure_id"].isin(checked_days)
        ]
        if len(pool):
            draws = rng.random(len(pool)) < RANDOM_PI
            picked = pool[draws].head(max(random_budget, 0))
        else:
            picked = pool
        for row in picked.itertuples():
            tasks.append({
                "station_id": station_id, "kind": "random", "event_id": None,
                "exposure_id": row.exposure_id, "pi": RANDOM_PI,
                "note": f"登记抽查 π={RANDOM_PI}",
            })
        if len(picked) < random_budget:
            notes.append(f"{station_id}：无告警组日池不足，抽查 {len(picked)}/{random_budget} 项")

    columns = ["station_id", "kind", "event_id", "exposure_id", "pi", "note"]
    frame = pd.DataFrame(tasks, columns=columns)
    record = {
        "plan_date": str(plan_date),
        "cutoff": cutoff.isoformat(),
        "tasks": tasks,
        "notes": notes,
        "targeted_owed": int(len(alarm_pending)),
        "fact_digest": contract.fact_digest(),
    }
    if ledger is not None:
        ledger.append("plan", record)
    return {"tasks": frame, "notes": notes, "record": record}
