"""台账：每次查看、决定、计划与复核的留痕。

- 每次决定记录观察截止点、采用的代码映射摘要与核实事实摘要；
- 查看分登记查看与临时查看，临时查看不制造更确定的证据；
- 策略（范围/门槛/核实策略）变化开启新纪元（epoch），原试用结论与
  新策略的后续观察分开报告，不混池。
"""
from __future__ import annotations

from pathlib import Path
import datetime as dt
import hashlib
import json

EPOCH_BASE = 1


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.records = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.records.append(json.loads(line))

    def append(self, kind: str, payload: dict) -> dict:
        record = {"kind": kind, "logged_at": _now(), **payload}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self.records.append(record)
        return record

    def of_kind(self, kind: str) -> list:
        return [r for r in self.records if r["kind"] == kind]

    def look_count(self) -> int:
        """已发生的查看次数（登记+临时），用于多重查看的保守调整。"""
        return len(self.of_kind("look")) + len(self.of_kind("decision"))

    def current_epoch(self, strategy_key: str) -> int:
        """策略键变化即新纪元；同键延续当前纪元。"""
        decisions = self.of_kind("decision")
        if not decisions:
            return EPOCH_BASE
        epochs = {d["epoch"]: d["strategy_key"] for d in decisions}
        latest = decisions[-1]
        if latest["strategy_key"] == strategy_key:
            return latest["epoch"]
        if strategy_key in epochs.values():
            return [k for k, v in epochs.items() if v == strategy_key][-1]
        return max(epochs) + 1


def strategy_key(requests) -> str:
    """扩围策略指纹：范围、风险增量、打扰目标与容量口径。"""
    items = sorted(
        (r.station_id, int(r.units), float(r.safety_increment), float(r.nuisance_reduction))
        for r in requests
    )
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()[:16]
