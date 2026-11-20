"""资料契约装载：类型解析、观察截止过滤与事实摘要。

规则（见 docs/资料口径.md）：
- 空值表示未知，不当作零；pending 的核实结果为空。
- available_at 是事实最早可知时刻；任何分析只能使用
  available_at <= 观察截止 的行，历史截点不得使用更晚才知的值。
- assignment 是计划版本，resident 是实际版本，两者各自保留含义。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import datetime as dt
import hashlib
import json

import pandas as pd

from sourcepack.catalog import inspect as inspect_pack

TZ = dt.timezone(dt.timedelta(hours=8))


def parse_ts(value: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("时刻缺少时区：" + value)
    return stamp


def parse_bool(value: str):
    """空 -> None（未知）；true/false -> 布尔。未知不等于阴性。"""
    if value is None or value == "":
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("布尔取值无效：" + str(value))


def _optional_float(value: str):
    return None if value in (None, "") else float(value)


@dataclass
class Contract:
    """一次观察截止下可见的全部事实。"""

    cutoff: dt.datetime
    code_map: pd.DataFrame          # version, code, family
    stations: pd.DataFrame          # station_id, region, checks_daily, action_minutes_daily
    assignment: pd.DataFrame        # 计划版本（ITT 口径）
    resident: pd.DataFrame          # 实际驻留（as-treated 口径）
    alarms: pd.DataFrame            # 原始报送（可含同一事件的重复报送）
    verifications: pd.DataFrame     # 核实任务；need_action 可空（未知）
    interventions: pd.DataFrame     # 严重事件后的干预/回退
    rollout_requests: pd.DataFrame  # 扩围申请（不受截止过滤，属行动请求）
    review_policy: pd.DataFrame     # 事先登记的查看
    digests: dict = field(default_factory=dict)  # 各表内容摘要，用于决定留痕

    def fact_digest(self) -> str:
        """本次截止下全部输入事实的联合摘要，决定留痕用。"""
        joined = json.dumps(self.digests, sort_keys=True)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _typed_frames(records: dict) -> dict:
    frames = {}
    frames["code_map"] = pd.DataFrame(records["code_map"],
                                      columns=["version", "code", "family"])

    stations = pd.DataFrame(records["stations"])
    stations["region"] = stations["region"].astype(int)
    stations["checks_daily"] = stations["checks_daily"].astype(int)
    stations["action_minutes_daily"] = stations["action_minutes_daily"].astype(int)
    frames["stations"] = stations

    assignment = pd.DataFrame(records["assignment"])
    assignment["date"] = assignment["date"].map(dt.date.fromisoformat)
    assignment["group_id"] = assignment["group_id"].astype(int)
    for col in ("registered_at", "available_at"):
        assignment[col] = assignment[col].map(parse_ts)
    frames["assignment"] = assignment

    resident = pd.DataFrame(records["resident"])
    resident["run_hours"] = resident["run_hours"].astype(float)
    resident["load_index"] = resident["load_index"].astype(float)
    resident["age_days"] = resident["age_days"].astype(int)
    resident["available_at"] = resident["available_at"].map(parse_ts)
    frames["resident"] = resident

    alarms = pd.DataFrame(records["alarms"])
    for col in ("occurred_at", "available_at"):
        alarms[col] = alarms[col].map(parse_ts)
    frames["alarms"] = alarms

    verifications = pd.DataFrame(records["verifications"])
    verifications["probability"] = verifications["probability"].map(_optional_float)
    verifications["need_action"] = verifications["need_action"].map(parse_bool)
    verifications["delay_minutes"] = verifications["delay_minutes"].map(_optional_float)
    verifications["checked_at"] = verifications["checked_at"].map(parse_ts)
    verifications["available_at"] = verifications["available_at"].map(parse_ts)
    frames["verifications"] = verifications

    interventions = pd.DataFrame(
        records["interventions"],
        columns=["intervention_id", "exposure_id", "started_at", "kind", "hours", "available_at"])
    if len(interventions):
        interventions["started_at"] = interventions["started_at"].map(parse_ts)
        interventions["hours"] = interventions["hours"].astype(float)
        interventions["available_at"] = interventions["available_at"].map(parse_ts)
    else:
        interventions = interventions.astype({"started_at": "datetime64[ns, UTC+08:00]",
                                              "hours": "float64",
                                              "available_at": "datetime64[ns, UTC+08:00]"})
    frames["interventions"] = interventions

    requests = pd.DataFrame(records["rollout_requests"])
    requests["units"] = requests["units"].astype(int)
    requests["safety_increment"] = requests["safety_increment"].astype(float)
    requests["nuisance_reduction"] = requests["nuisance_reduction"].astype(float)
    requests["cutoff"] = requests["cutoff"].map(parse_ts)
    requests["review_by"] = requests["review_by"].map(dt.date.fromisoformat)
    frames["rollout_requests"] = requests

    policy = pd.DataFrame(records["review_policy"])
    policy["registered_at"] = policy["registered_at"].map(parse_ts)
    policy["look_day"] = policy["look_day"].map(dt.date.fromisoformat)
    frames["review_policy"] = policy
    return frames


def _digest(frame: pd.DataFrame) -> str:
    raw = frame.to_csv(index=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_contract(root: Path, cutoff: dt.datetime | None = None) -> Contract:
    """装载资料并应用观察截止：只保留 cutoff 前已可知的行。

    cutoff 为 None 表示不做时间过滤（仅用于资料自检，不用于估计）。
    """
    root = Path(root).resolve()
    report = inspect_pack(root)  # 完整性先决：摘要、类型、键、引用
    if report["status"] != "ok":
        raise ValueError("资料检查未通过")

    import csv

    records = {}
    catalog = json.loads((root / "data/catalog.json").read_text(encoding="utf-8"))
    for table in catalog["tables"]:
        with (root / table["path"]).open(encoding="utf-8", newline="") as stream:
            records[table["name"]] = list(csv.DictReader(stream))
    frames = _typed_frames(records)

    if cutoff is None:
        cutoff = dt.datetime.max.replace(tzinfo=TZ)

    visible = {}
    for name, frame in frames.items():
        if name in ("rollout_requests", "review_policy", "stations", "code_map"):
            visible[name] = frame  # 请求、登记与静态资料不按事实截止过滤
        elif "available_at" in frame.columns:
            visible[name] = frame[frame["available_at"] <= cutoff].copy()
        else:
            visible[name] = frame.copy()

    digests = {name: _digest(frame) for name, frame in visible.items()}
    return Contract(cutoff=cutoff, digests=digests, **visible)
