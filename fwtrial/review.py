"""复核触发：后来才知的事故核实对照仍有效的扩围建议。

- 决定只使用其观察截止点前可知的事实；之后到达的核实结果若需处置，
  且落在决定覆盖的站，则该决定需要复核。
- 截止时待定（pending）或之后登记但仍未执行的核实，其所在站的决定
  列为"待复核观察项"：结论依赖未决事实。
- 复核记录按（决定、触发核实集合）去重，重复运行不污染台账。
"""
from __future__ import annotations

from .contract import load_contract, parse_ts
from .estimate import dedup_checks


def find_reviews(root, ledger) -> dict:
    """对台账中每个 expand/expand_limited 决定给出复核标记。"""
    decisions = [r for r in ledger.of_kind("decision") if r["decision"] in ("expand", "expand_limited")]
    if not decisions:
        return {"decisions_checked": 0, "reviews": []}
    contract = load_contract(root)  # 当前全部可知事实
    checks = dedup_checks(contract.verifications)
    exposure = contract.assignment[["exposure_id", "station_id"]]
    checks = checks.merge(exposure, on="exposure_id", how="left")

    reviews = []
    for record in decisions:
        cutoff = parse_ts(record["cutoff"])
        station = record["station_id"]
        here = checks[checks["station_id"] == station]
        triggers = []
        # 截止后才可知的阳性核实：直接触发复核
        late_positive = here[(here["available_at"] > cutoff) & (here["need_action"] == True)]  # noqa: E712
        for row in late_positive.itertuples():
            triggers.append({
                "check_id": row.check_id, "event_id": row.event_id,
                "reason": "截止后核实确认需处置",
                "available_at": row.available_at.isoformat(),
            })
        # 至今仍悬而未决的核实：结论依赖未决事实
        pending = here[~here["executed"]]
        for row in pending.itertuples():
            known = "截止前已登记" if row.available_at <= cutoff else "截止后才登记"
            triggers.append({
                "check_id": row.check_id, "event_id": row.event_id,
                "reason": f"核实{known}但至今未执行，结论依赖未决事实",
                "available_at": None,
            })
        if triggers:
            reviews.append({
                "request_id": record["request_id"],
                "station_id": station,
                "decision": record["decision"],
                "cutoff": record["cutoff"],
                "status": "需要复核" if len(late_positive) else "待复核观察项",
                "triggers": triggers,
            })
    existing = {
        (r["request_id"], tuple(sorted(t["check_id"] for t in r["triggers"])))
        for r in ledger.of_kind("review")
    }
    for review in reviews:
        key = (review["request_id"], tuple(sorted(t["check_id"] for t in review["triggers"])))
        if key not in existing:
            ledger.append("review", review)
    return {"decisions_checked": len(decisions), "reviews": reviews}
