"""命令行入口：分析、登记查看、扩围决策、核实计划、复核与回查。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from .contract import TZ, load_contract
from .decide import evaluate_requests
from .ledger import Ledger
from .looks import registered_looks
from .plan import plan_checks
from .report import (analyze, format_analysis, format_decisions, format_plan,
                     format_reviews, trace)
from .review import find_reviews


def _cutoff(text: str) -> dt.datetime:
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=TZ)
    return stamp


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fwtrial", description="固件试用效果判定与扩围控制")
    parser.add_argument("--root", default=".", help="资料根目录（含 data/catalog.json）")
    parser.add_argument("--state", default="out/ledger.jsonl", help="台账文件")
    parser.add_argument("-B", type=int, default=500, help="整群 bootstrap 次数")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("analyze", help="效果与不确定性分析").add_argument(
        "--cutoff", required=True, help="观察截止，如 2025-01-30T08:00:00+08:00")
    sub.add_parser("looks", help="登记查看轨迹（α 消耗）")
    sub.add_parser("decide", help="扩围申请评估并留痕")
    sub.add_parser("plan", help="次日核实安排").add_argument("--date", required=True)
    sub.add_parser("review", help="后期事实触发的复核")
    sub.add_parser("trace", help="扩围决定回查").add_argument("request_id")
    sub.add_parser("demo", help="全流程演示（合成资料）")

    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    state = args.state
    if args.command == "demo" and args.state == "out/ledger.jsonl":
        state = "out/demo-ledger.jsonl"  # 演示不污染正式台账
    ledger = Ledger(root / state)

    if args.command == "analyze":
        result = analyze(root, _cutoff(args.cutoff), B=args.B)
        print(format_analysis(result))
    elif args.command == "looks":
        contract = load_contract(root)
        results = registered_looks(root, contract.review_policy, ledger=ledger, B=args.B)
        for r in results:
            print(f"{r['look_day']}（{r['policy_id']}，信息量 {r['information_fraction']:.2f}，"
                  f"α 增量 {r['alpha_increment']:.4f}）：主要指标 {r['primary_metric']} "
                  f"差值 {r['estimate']:.4f} 区间 [{r['interval'][0]:.4f}, {r['interval'][1]:.4f}]"
                  f"{'，越界' if r['crossed'] else ''}")
    elif args.command == "decide":
        decisions = evaluate_requests(root, ledger, B=args.B)
        print(format_decisions(decisions))
    elif args.command == "plan":
        plan = plan_checks(root, dt.date.fromisoformat(args.date), ledger=ledger)
        print(format_plan(plan))
    elif args.command == "review":
        print(format_reviews(find_reviews(root, ledger)))
    elif args.command == "trace":
        print(trace(root, ledger, args.request_id))
    elif args.command == "demo":
        cutoff = "2025-01-30T08:00:00+08:00"
        print("=" * 30, "1/5 效果分析", "=" * 30)
        print(format_analysis(analyze(root, _cutoff(cutoff), B=args.B)))
        print("=" * 30, "2/5 登记查看", "=" * 30)
        contract = load_contract(root)
        for r in registered_looks(root, contract.review_policy, ledger=ledger, B=args.B):
            print(f"{r['look_day']}: 差值 {r['estimate']:.4f} α增量 {r['alpha_increment']:.4f}")
        print("=" * 30, "3/5 扩围决策", "=" * 30)
        print(format_decisions(evaluate_requests(root, ledger, B=args.B)))
        print("=" * 30, "4/5 核实安排", "=" * 30)
        print(format_plan(plan_checks(root, dt.date(2025, 1, 31), ledger=ledger)))
        print("=" * 30, "5/5 复核触发", "=" * 30)
        print(format_reviews(find_reviews(root, ledger)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
