"""分析报告：效果、不确定性、可比范围与不可识别部分（中文文本）。"""
from __future__ import annotations

import datetime as dt

from .contract import load_contract
from .estimate import (build_station_frames, cluster_bootstrap, compute_metrics,
                       covariate_balance, standardized_nuisance, station_icc)
from .events import eventize, rename_audit
from .exposure import build_exposure, exposure_summary

LIMITATIONS = """统计限制（安全负责人与值班人员共读）：
1. 站是最小独立单位：同站两组共享班组与功率控制，一组告警减少可能使另一组
   得到更及时处置。组间对比估计的是"存在外溢时的系统级效果"，无干扰的
   设备级效果不可识别；不确定性按站级整群 bootstrap 计算，站数少时区间很宽。
2. 待核实与未检查不算阴性：它们以识别界（最好/最坏情形）呈现，界宽即
   不可识别部分；未执行的计划不进入任何分母，重复核查不增加样本量。
3. 无告警群体只有登记抽查能观察；抽查概率变化会改变权重，跨期比较须用
   同一登记概率或重新加权。
4. 代码改名不算改善：所有计数在可比语义（family）层进行。
5. 回退/干预后的平静时段不计入新版收益；该时段在实际口径下属旧版暴露，
   在计划口径下是策略成本。
6. 负荷与设备年龄随时间变化：给出粗率与负荷标准化率，两者不一致时以
   标准化率为准并检查均衡性表。
7. 反复查看不制造证据：只有登记查看产出决策级结论，临时查看的区间按
   查看次数加宽。
8. 处置分钟以处置延迟为代理，非实际工时；容量判断偏保守。"""


def analyze(root, cutoff: dt.datetime, B: int = 1000, level: float = 0.95) -> dict:
    contract = load_contract(root, cutoff)
    exposure = build_exposure(contract)
    events = eventize(contract.alarms, contract.resident, contract.code_map)
    result = {"cutoff": cutoff.isoformat(), "exposure": exposure_summary(exposure)}
    result["eventization"] = {
        "events": int(len(events)),
        "messages": int(len(contract.alarms)),
        "duplicated_messages": int((events["n_messages"] - 1).sum()),
        "merged_events": int((events["n_family"] > 1).sum()),
        "kinds": events["kind"].value_counts().to_dict(),
    }
    audit = rename_audit(contract.alarms, contract.resident, contract.code_map)
    result["rename_audit"] = {"renamed_families": audit["renamed_families"], "note": audit["note"]}

    arms = {}
    for arm in ("resident", "assigned"):
        station_map = build_station_frames(contract, arm=arm)
        arms[arm] = {
            "metrics": compute_metrics(list(station_map.values())),
            "bootstrap": cluster_bootstrap(station_map, B=B, level=level),
            "icc": station_icc(station_map),
            "balance": covariate_balance(station_map).to_dict(),
        }
    result["arms"] = arms
    result["standardized"] = standardized_nuisance(
        list(build_station_frames(contract, arm="resident").values()))
    return result


def _fmt_interval(interval) -> str:
    lo, hi = interval
    try:
        return f"[{lo:.4f}, {hi:.4f}]"
    except (TypeError, ValueError):
        return "[不可估]"


def format_analysis(result: dict) -> str:
    lines = [f"观察截止：{result['cutoff']}", ""]
    ev = result["eventization"]
    lines.append(f"事件化：报送 {ev['messages']} 条 → 事件 {ev['events']} 起"
                 f"（重复报送 {ev['duplicated_messages']} 条，合并告警 {ev['merged_events']} 起）")
    renamed = result["rename_audit"]["renamed_families"]
    if renamed:
        pairs = "；".join(f"{r['family']}: {r['codes']}" for r in renamed)
        lines.append(f"代码改名审计：{pairs} —— 改名本身不算改善")
    exp = result["exposure"]
    lines.append(f"暴露小时（实际口径）：{exp['hours_by_arm']['resident']}；"
                 f"（计划口径）：{exp['hours_by_arm']['assigned']}")
    lines.append(f"安排与实际不一致组日 {exp['mismatch_days']} 天（分站 {exp['mismatch_by_station']}）；"
                 f"干预日 {exp['intervention_days']} 天；回退后平静时段 "
                 f"{exp['post_rollback_quiet_days']} 天 / {exp['post_rollback_quiet_hours']} 小时"
                 f"（不计入新版收益）")
    for arm, label in (("resident", "实际口径"), ("assigned", "计划口径")):
        block = result["arms"][arm]
        m = block["metrics"]
        new, old, diffs = m["per_version"]["new"], m["per_version"]["old"], m["diffs"]
        boot = block["bootstrap"]
        lines += ["", f"—— {label}（区间水平 {boot['level']:.0%}）——",
                  f"无效打扰率（次/小时）：new {new['nuisance_rate']:.4f} "
                  f"识别界[{new['nuisance_bound_lo']:.4f},{new['nuisance_bound_hi']:.4f}]；"
                  f"old {old['nuisance_rate']:.4f} "
                  f"识别界[{old['nuisance_bound_lo']:.4f},{old['nuisance_bound_hi']:.4f}]",
                  f"  相对减少 {diffs['nuisance_reduction_rel']:.1%} "
                  f"区间{_fmt_interval(boot['intervals']['nuisance_reduction_rel'])}",
                  f"漏处置风险（次/小时）：new {new['missed_rate']:.4f}（上界 "
                  f"{new['missed_bound_hi']:.4f}）；old {old['missed_rate']:.4f}（上界 {old['missed_bound_hi']:.4f}）",
                  f"  差值 {diffs['missed_rate_diff']:.4f} 区间{_fmt_interval(boot['intervals']['missed_rate_diff'])}",
                  f"处置延迟中位（分）：new {new['delay_median']:.0f}；old {old['delay_median']:.0f}；"
                  f"差值区间{_fmt_interval(boot['intervals']['delay_median_diff'])}",
                  f"严重事件率（次/小时）：new {new['severe_rate']:.4f}；old {old['severe_rate']:.4f}；"
                  f"差值区间{_fmt_interval(boot['intervals']['severe_rate_diff'])}",
                  f"站内关联：ICC {block['icc']['icc']}，设计效应 {block['icc']['deff']}，"
                  f"有效样本量 {block['icc']['effective_n']}（站数 {block['icc']['clusters']}）",
                  f"待核实 {new['pending_checks'] + old['pending_checks']} 项；"
                  f"重复核查剔除 {new['rechecks_dropped'] + old['rechecks_dropped']} 项；"
                  f"未核实告警事件 {new['unverified_alarm_events'] + old['unverified_alarm_events']} 起"]
    std = result["standardized"]["standardized_nuisance_rate"]
    lines += ["", f"负荷标准化无效打扰率：new {std['new']:.4f}；old {std['old']:.4f}"
              f"（分层 {result['standardized']['strata']}）"]
    lines += ["", LIMITATIONS]
    return "\n".join(lines)


def format_decisions(decisions: list) -> str:
    labels = {"expand": "支持扩围", "expand_limited": "限定规模扩围",
              "continue_trial": "继续试用", "pause": "暂停"}
    lines = []
    for d in decisions:
        head = (f"{d['request_id']}（站 {d['station_id']}，申请 {d['requested_units']} 台）："
                f"{labels[d['decision']]}")
        if d["decision"] in ("expand", "expand_limited"):
            head += f"，建议规模 {d['recommended_units']} 台"
        lines.append(head + f"（epoch {d['epoch']}，置信水平 {d['confidence_level']:.3f}）")
        for reason in d["reasons"]:
            lines.append(f"  - {reason}")
        lines.append(f"  高风险事件排除数 {d['high_risk_events_excluded']}（护栏：恒为 0）；"
                     f"需处置事件全部计入（{d['acted_events_counted']} 起）")
    return "\n".join(lines)


def format_plan(plan: dict) -> str:
    lines = [f"核实任务（计划日 {plan['record']['plan_date']}，值班人员执行，"
             f"结果须回流并带登记概率）："]
    tasks = plan["tasks"]
    if len(tasks):
        for station, sub in tasks.groupby("station_id"):
            lines.append(f"  {station}：")
            for row in sub.itertuples():
                target = row.event_id if row.event_id else f"组日 {row.exposure_id}"
                lines.append(f"    [{row.kind}] {target}（{row.note}）")
    else:
        lines.append("  （无待办）")
    for note in plan["notes"]:
        lines.append(f"  注意：{note}")
    return "\n".join(lines)


def format_reviews(result: dict) -> str:
    if not result["decisions_checked"]:
        return "台账中没有仍有效的扩围决定，无从复核。"
    reviews = result["reviews"]
    if not reviews:
        return f"已核对 {result['decisions_checked']} 项仍有效的扩围建议：无复核触发。"
    lines = [f"复核触发（已核对 {result['decisions_checked']} 项决定）："]
    for r in reviews:
        lines.append(f"  {r['request_id']}（站 {r['station_id']}，{r['decision']}，"
                     f"截止 {r['cutoff']}）：{r['status']}")
        for t in r["triggers"]:
            lines.append(f"    - {t['check_id']} / {t['event_id']}：{t['reason']}")
    return "\n".join(lines)


def trace(root, ledger, request_id: str) -> str:
    """从任一扩围决定回查：试用安排、核实落实与后续复核。"""
    decisions = [r for r in ledger.of_kind("decision") if r["request_id"] == request_id]
    if not decisions:
        return f"台账中无 {request_id} 的决定记录。"
    lines = []
    for d in decisions:
        lines += [f"决定 {d['request_id']}（epoch {d['epoch']}）：",
                  f"  观察截止 {d['cutoff']}；置信水平 {d['confidence_level']}"
                  f"（按 {d['looks_adjusted_for']} 次查看调整）",
                  f"  代码映射摘要 {d['code_map_digest'][:12]}…；事实摘要 {d['fact_digest'][:12]}…",
                  f"  结论 {d['decision']}，建议规模 {d['recommended_units']} 台",
                  f"  关键指标（实际口径）：{d['metrics']['diffs']}"]
        # 回查该站截止时的试用安排与核实落实
        try:
            from .contract import load_contract, parse_ts
            from .estimate import dedup_checks
            from .exposure import build_exposure
            contract = load_contract(root, parse_ts(d["cutoff"]))
            exposure = build_exposure(contract)
            st = exposure[exposure["station_id"] == d["station_id"]]
            assigned = st.groupby("assigned_version")["run_hours"].sum().to_dict()
            resident = st.groupby("resident_version")["run_hours"].sum().to_dict()
            checks = dedup_checks(contract.verifications)
            checks = checks[checks["exposure_id"].isin(set(st["exposure_id"]))]
            done = checks[checks["executed"]]
            lines += [
                f"  试用安排（{d['station_id']} 截止时）：安排小时 {assigned}；实际驻留 {resident}；"
                f"安排与实际不一致 {int((st['assigned_version'] != st['resident_version']).sum())} 天；"
                f"干预 {int(st['intervention'].sum())} 天",
                f"  核实落实：已执行定向 {int((done['selection'] == 'targeted').sum())} 项、"
                f"抽查 {int((done['selection'] == 'random').sum())} 项；"
                f"待执行 {int((~checks['executed']).sum())} 项；"
                f"重复核查剔除 {int(checks['recheck'].sum())} 项"]
        except ValueError as exc:  # 资料已变化，留痕摘要用于对账
            lines.append(f"  试用安排回查失败（资料与留痕摘要不符）：{exc}")
    reviews = [r for r in ledger.of_kind("review") if r["request_id"] == request_id]
    for r in reviews:
        lines.append(f"  后续复核：{r['status']}（{len(r['triggers'])} 项触发）")
    plans = ledger.of_kind("plan")
    if plans:
        lines.append(f"  相关核实计划 {len(plans)} 份（最近 {plans[-1]['plan_date']}）")
    return "\n".join(lines)
