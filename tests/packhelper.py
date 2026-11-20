"""测试用迷你资料包生成器：与正式目录相同的契约，摘要按当前文件计算。"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

TZ = "+08:00"

SCHEMA = {
    "code_map": (["version", "code", "family"], ["version", "code"], []),
    "stations": (["station_id", "region", "checks_daily", "action_minutes_daily"], ["station_id"], []),
    "assignment": (["exposure_id", "station_id", "group_id", "date", "assigned_version",
                    "registered_at", "available_at"], ["exposure_id"],
                   [{"column": "station_id", "target_table": "stations", "target_column": "station_id"}]),
    "resident": (["exposure_id", "resident_version", "run_hours", "load_index", "age_days",
                  "available_at"], ["exposure_id"],
                 [{"column": "exposure_id", "target_table": "assignment", "target_column": "exposure_id"}]),
    "alarms": (["message_id", "event_id", "exposure_id", "code", "occurred_at", "available_at"],
               ["message_id"],
               [{"column": "exposure_id", "target_table": "assignment", "target_column": "exposure_id"}]),
    "verifications": (["check_id", "event_id", "exposure_id", "selection", "probability",
                       "checked_at", "need_action", "delay_minutes", "available_at"], ["check_id"],
                      [{"column": "exposure_id", "target_table": "assignment", "target_column": "exposure_id"}]),
    "interventions": (["intervention_id", "exposure_id", "started_at", "kind", "hours", "available_at"],
                      ["intervention_id"],
                      [{"column": "exposure_id", "target_table": "assignment", "target_column": "exposure_id"}]),
    "rollout_requests": (["request_id", "station_id", "units", "safety_increment",
                          "nuisance_reduction", "cutoff", "review_by"], ["request_id"],
                         [{"column": "station_id", "target_table": "stations", "target_column": "station_id"}]),
    "review_policy": (["policy_id", "registered_at", "look_day", "primary_metric", "scope"],
                      ["policy_id", "look_day"], []),
}

TYPES = {
    "region": "integer", "checks_daily": "integer", "action_minutes_daily": "integer",
    "group_id": "integer", "date": "date", "registered_at": "datetime",
    "available_at": "datetime", "run_hours": "number", "load_index": "number",
    "age_days": "integer", "occurred_at": "datetime", "probability": "number",
    "checked_at": "datetime", "delay_minutes": "number", "started_at": "datetime",
    "hours": "number", "units": "integer", "safety_increment": "number",
    "nuisance_reduction": "number", "cutoff": "datetime", "review_by": "date",
    "look_day": "date", "need_action": "boolean",
}
NULLABLE = {"probability", "need_action", "delay_minutes"}


def avail(day: int) -> str:
    return f"2025-01-{day + 1:02d}T08:00:00{TZ}"


def mini_tables() -> dict:
    """2 站 × 2 组 × 4 日的基线资料；各测试可在此之上覆盖。"""
    days = [f"2025-01-{d:02d}" for d in range(1, 5)]
    assignment, resident = [], []
    plan = {("S1", 0): "new", ("S1", 1): "old", ("S2", 0): "old", ("S2", 1): "new"}
    idx = {"S1": 1, "S2": 2}
    for (station, group), version in plan.items():
        for d, date in enumerate(days):
            eid = f"E{d}-{idx[station]}-{group}"
            assignment.append([eid, station, group, date, version,
                               f"2024-12-28T08:00:00{TZ}", avail(d)])
            actual = "old" if (station, group, d) == ("S2", 1, 2) else version  # 一次升级失败
            resident.append([eid, actual, "20", "1.0", "100", avail(d)])
    alarms = [
        # 旧版：3 起无效 + 1 起需处置；含重复报送
        ["EV0-1-1-0", "EV0-1-1", "E0-1-1", "A1", f"2025-01-01T09:00:00{TZ}", avail(0)],
        ["EV0-1-1-1", "EV0-1-1", "E0-1-1", "A1", f"2025-01-01T10:00:00{TZ}", avail(0)],
        ["EV1-1-1-0", "EV1-1-1", "E1-1-1", "A1", f"2025-01-02T09:00:00{TZ}", avail(1)],
        ["EV2-1-1-0", "EV2-1-1", "E2-1-1", "A1", f"2025-01-03T09:00:00{TZ}", avail(2)],
        ["EV3-1-1-0", "EV3-1-1", "E3-1-1", "A1", f"2025-01-04T09:00:00{TZ}", avail(3)],
        # 新版：1 起无效
        ["EV0-2-1-0", "EV0-2-1", "E0-2-1", "T01", f"2025-01-01T09:00:00{TZ}", avail(0)],
    ]
    verifications = [
        ["QEV0-1-1", "EV0-1-1", "E0-1-1", "targeted", "", f"2025-01-01T12:00:00{TZ}", "false", "30", avail(1)],
        ["QEV1-1-1", "EV1-1-1", "E1-1-1", "targeted", "", f"2025-01-02T12:00:00{TZ}", "true", "40", avail(2)],
        ["QEV2-1-1", "EV2-1-1", "E2-1-1", "targeted", "", f"2025-01-03T12:00:00{TZ}", "false", "35", avail(3)],
        ["QEV3-1-1", "EV3-1-1", "E3-1-1", "targeted", "", f"2025-01-04T12:00:00{TZ}", "false", "35", avail(3)],
        ["QEV0-2-1", "EV0-2-1", "E0-2-1", "targeted", "", f"2025-01-01T12:00:00{TZ}", "false", "35", avail(1)],
        # 登记抽查：旧版无告警日 1 起需处置（漏报），其余阴性
        ["QR1", "EV0-1-0", "E0-1-0", "random", "0.2", f"2025-01-01T15:00:00{TZ}", "false", "", avail(1)],
        ["QR2", "EV1-1-0", "E1-1-0", "random", "0.2", f"2025-01-02T15:00:00{TZ}", "false", "", avail(2)],
        ["QR3", "EV2-1-0", "E2-1-0", "random", "0.2", f"2025-01-03T15:00:00{TZ}", "false", "", avail(3)],
        ["QR4", "EV1-2-0", "E1-2-0", "random", "0.2", f"2025-01-02T15:00:00{TZ}", "true", "50", avail(2)],
        ["QR5", "EV2-2-0", "E2-2-0", "random", "0.2", f"2025-01-03T15:00:00{TZ}", "false", "", avail(3)],
        ["QR6", "EV1-2-1", "E1-2-1", "random", "0.2", f"2025-01-02T15:00:00{TZ}", "false", "", avail(2)],
        ["QR7", "EV3-2-1", "E3-2-1", "random", "0.2", f"2025-01-04T15:00:00{TZ}", "false", "", avail(3)],
    ]
    tables = {
        "code_map": [["old", "A1", "thermal"], ["new", "T01", "thermal"]],
        "stations": [["S1", "1", "3", "50"], ["S2", "0", "3", "50"]],
        "assignment": assignment,
        "resident": resident,
        "alarms": alarms,
        "verifications": verifications,
        "interventions": [],
        "rollout_requests": [["R1", "S1", "4", "0.05", "0.10",
                              f"2025-01-05T08:00:00{TZ}", "2025-01-10"]],
        "review_policy": [["P1", f"2024-12-28T08:00:00{TZ}", "2025-01-03", "need_action_per_hour", "all"],
                          ["P1", f"2024-12-28T08:00:00{TZ}", "2025-01-05", "need_action_per_hour", "all"]],
    }
    return tables


def write_pack(root: Path, tables: dict) -> Path:
    """把表字典写成带目录与摘要的资料包。"""
    root = Path(root)
    (root / "data").mkdir(parents=True, exist_ok=True)
    catalog = {"title": "mini", "synthetic": True, "tables": []}
    for name, (columns, key, references) in SCHEMA.items():
        rows = tables[name]
        path = root / "data" / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
        cols = []
        for col in columns:
            spec = {"name": col, "type": TYPES.get(col, "string"), "description": col}
            if col in NULLABLE:
                spec["nullable"] = True
            cols.append(spec)
        catalog["tables"].append({
            "name": name, "description": name, "columns": cols, "key": key,
            "references": references, "path": f"data/{name}.csv",
            "row_count": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    (root / "data" / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return root
