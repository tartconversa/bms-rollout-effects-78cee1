"""事件化：把原始报送整理为可比较的事件。

三类区分：
- 重复报送：同一 event_id 的多条 message，只算一个事件；
- 合并告警：同一事件覆盖多个可比语义（family），按 family 展开计数；
- 真实独立事件：不同 event_id。

代码改名不算改善：所有计数在 family 层进行，code 层的增减只作审计。
"""
from __future__ import annotations

import pandas as pd


def attach_family(alarms: pd.DataFrame, resident: pd.DataFrame, code_map: pd.DataFrame) -> pd.DataFrame:
    """按发出告警的*实际驻留版本*映射可比语义。"""
    version_by_exposure = resident.set_index("exposure_id")["resident_version"]
    family_by_vc = {(r.version, r.code): r.family for r in code_map.itertuples()}
    frame = alarms.copy()
    frame["version"] = frame["exposure_id"].map(version_by_exposure)
    frame["family"] = [family_by_vc.get((v, c)) for v, c in zip(frame["version"], frame["code"])]
    if frame["family"].isna().any():
        missing = frame[frame["family"].isna()][["version", "code"]].drop_duplicates()
        raise ValueError("告警代码不在资料契约映射中：" + missing.to_string(index=False))
    return frame


def eventize(alarms: pd.DataFrame, resident: pd.DataFrame, code_map: pd.DataFrame) -> pd.DataFrame:
    """每个 event_id 一行：消息数、涉及 family、首发时刻。"""
    frame = attach_family(alarms, resident, code_map)
    grouped = frame.groupby("event_id", sort=False)
    events = grouped.agg(
        exposure_id=("exposure_id", "first"),
        version=("version", "first"),
        n_messages=("message_id", "count"),
        n_codes=("code", "nunique"),
        families=("family", lambda s: tuple(sorted(set(s)))),
        first_at=("occurred_at", "min"),
    ).reset_index()
    events["n_family"] = events["families"].map(len)

    def kind(row) -> str:
        if row["n_family"] > 1:
            return "merged"       # 合并告警：一条报送覆盖多个语义
        if row["n_messages"] > 1:
            return "duplicated"   # 重复报送：同一疑点多次上报
        return "independent"      # 真实独立事件

    events["kind"] = events.apply(kind, axis=1)
    return events


def family_events(events: pd.DataFrame) -> pd.DataFrame:
    """合并告警按 family 展开，便于跨版本在语义层计数。"""
    rows = []
    for row in events.itertuples():
        for family in row.families:
            rows.append({"event_id": row.event_id, "exposure_id": row.exposure_id,
                         "version": row.version, "family": family})
    return pd.DataFrame(rows)


def rename_audit(alarms: pd.DataFrame, resident: pd.DataFrame, code_map: pd.DataFrame) -> dict:
    """代码改名审计：同一 family 内 code 层面的计数变化不得解释为改善。"""
    frame = attach_family(alarms, resident, code_map)
    by_code = frame.groupby(["version", "code", "family"]).size().rename("messages").reset_index()
    renames = []
    for family, sub in code_map.groupby("family"):
        codes = {r.version: r.code for r in sub.itertuples()}
        if len(set(codes.values())) > 1:
            renames.append({"family": family, "codes": codes})
    return {
        "by_code": by_code,
        "renamed_families": renames,
        "note": "family 层计数才可比较；code 层差异含改名噪声，不算改善。",
    }
