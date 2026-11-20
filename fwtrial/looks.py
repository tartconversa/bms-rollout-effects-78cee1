"""登记查看与多重查看纪律。

- 主要指标与查看日在 review_policy 事先登记；只有登记查看产出
  决策级结论，并按 O'Brien–Fleming 型 α 消耗给出逐次边界。
- 临时查看（未登记的截止）只给描述性结果，区间按已发生查看次数
  做 Bonferroni 加宽；反复查看不会让证据越来越确定。
- 每次查看都写入台账，信息量以累计有效暴露小时计。
"""
from __future__ import annotations

import datetime as dt

import numpy as np
from scipy import stats

from .contract import TZ, load_contract
from .estimate import build_station_frames, cluster_bootstrap, compute_metrics
from .params import ALPHA_TOTAL


def look_cutoff(look_day) -> dt.datetime:
    """查看日 08:00（+08:00）：与资料可知节奏一致，可见前一日的记录。"""
    return dt.datetime.combine(look_day, dt.time(8, 0), tzinfo=TZ)


def _alpha_spent(t: float) -> float:
    """OBF 型 α 消耗函数：alpha(t) = 2(1 - Phi(z_{a/2} / sqrt(t)))。"""
    if t <= 0:
        return 0.0
    z = stats.norm.ppf(1 - ALPHA_TOTAL / 2)
    return float(2 * (1 - stats.norm.cdf(z / np.sqrt(t))))


def registered_looks(root, policy, ledger=None, B: int = 500) -> list:
    """按登记查看日依次评估主要指标，返回逐次结果与边界。"""
    looks = sorted(policy.itertuples(), key=lambda r: r.look_day)
    cutoffs = [look_cutoff(r.look_day) for r in looks]
    # 信息量：累计有效暴露小时；以末次登记查看为 1。每个截止只装载一次。
    contracts = [load_contract(root, cutoff) for cutoff in cutoffs]
    totals = [float(c.resident["run_hours"].sum()) for c in contracts]
    final = totals[-1] if totals and totals[-1] > 0 else 1.0

    results = []
    spent_before = 0.0
    for row, cutoff, info, contract in zip(looks, cutoffs, totals, contracts):
        t = max(info / final, 1e-9)
        spent = _alpha_spent(t)
        incremental = max(spent - spent_before, 1e-12)
        spent_before = spent
        z_bound = float(stats.norm.ppf(1 - incremental / 2))

        station_map = build_station_frames(contract, arm="resident")
        metrics = compute_metrics(list(station_map.values()))
        boot = cluster_bootstrap(station_map, B=B, level=1 - incremental)
        lo, hi = boot["intervals"]["missed_rate_diff"]
        estimate = metrics["diffs"]["missed_rate_diff"]
        record = {
            "type": "registered",
            "policy_id": row.policy_id,
            "look_day": str(row.look_day),
            "cutoff": cutoff.isoformat(),
            "primary_metric": row.primary_metric,
            "scope": row.scope,
            "information_fraction": round(t, 4),
            "alpha_increment": round(incremental, 6),
            "z_boundary": round(z_bound, 3),
            "estimate": estimate,
            "interval": (lo, hi),
            "crossed": bool(np.isfinite(lo) and (lo > 0 or hi < 0)),
        }
        results.append(record)
        if ledger is not None:
            ledger.append("look", {**record, "interval": list(record["interval"])})
    return results


def adhoc_confidence(ledger) -> tuple:
    """临时查看的保守置信水平：对全部已发生查看做 Bonferroni。"""
    n = max(1, ledger.look_count() + 1)
    return 1 - ALPHA_TOTAL / n, n
