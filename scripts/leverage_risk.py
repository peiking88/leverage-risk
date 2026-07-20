#!/usr/bin/env python3
"""
市场杠杆风险监测 — 主板、创业板、科创板 融资余额/流通市值。

数据源:
  融资余额: AKShare (stock_margin_detail_szse/sse)
  流通市值: 用户提供 / 内置参考数据（交易所官方统计）

风险阈值: 融资余额 / 流通市值 > 4% 触发警示

用法:
  python3 leverage_risk.py                          # 使用内置参考流通市值
  python3 leverage_risk.py --date 20260717          # 指定融资余额日期
  python3 leverage_risk.py --json                  # JSON 输出
  python3 leverage_risk.py --cyb-mv 144934.15 --kcb-mv 107381.71  # 传入流通市值(亿元)
  python3 leverage_risk.py --sz-main-mv ... --sh-main-mv ...      # 主板流通市值
"""

import argparse
import json
import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

# ---- 配置 ----
RISK_THRESHOLD = 4.0

# 内置参考数据 (2026年7月17日 - 交易所官方统计)
# 流通市值单位: 亿元
REFERENCE_DATA = {
    "date": "20260717",
    "circulating_mv": {
        "创业板": 144934.15,
        "科创板": 107381.71,
        "深市主板": None,
        "沪市主板": None,
        "沪深两市": 822000.0,
    },
    "source": "交易所官方统计 (2026-07-17)",
}


# ---- AKShare 融资余额 ----
def get_latest_margin_date() -> str:
    """获取最近有融资融券数据的日期。"""
    today = datetime.now()
    for delta in range(7):
        d = today - timedelta(delta)
        ds = d.strftime("%Y%m%d")
        try:
            df = ak.stock_margin_detail_szse(date=ds)
            if len(df) > 0:
                return ds
        except Exception:
            continue
    raise RuntimeError("近7天无融资融券数据")


def fetch_margin_by_board(date_str: str) -> dict[str, float]:
    """
    获取融资融券明细并按板块分类汇总。
    返回 {板块名: 融资余额(元)}。
    """
    df_sz = ak.stock_margin_detail_szse(date=date_str)
    time.sleep(2)
    df_sh = ak.stock_margin_detail_sse(date=date_str)

    return {
        "创业板": float(df_sz[df_sz["证券代码"].str.startswith(("300", "301"))]["融资余额"].sum()),
        "科创板": float(df_sh[df_sh["标的证券代码"].str.startswith("688")]["融资余额"].sum()),
        "深市主板": float(df_sz[~df_sz["证券代码"].str.startswith(("300", "301"))]["融资余额"].sum()),
        "沪市主板": float(df_sh[~df_sh["标的证券代码"].str.startswith("688")]["融资余额"].sum()),
    }


# ---- 主分析 ----
def analyze(
    date_str: str | None = None,
    threshold: float = RISK_THRESHOLD,
    circulating_mv_overrides: dict[str, float] | None = None,
) -> dict:
    """
    执行杠杆风险分析。

    Args:
        date_str: 融资余额日期 (YYYYMMDD)
        threshold: 风险阈值 (%)
        circulating_mv_overrides: 用户指定的流通市值 (亿元)，覆盖内置参考
    """
    if not date_str:
        date_str = get_latest_margin_date()

    result = {
        "date": date_str,
        "threshold": threshold,
        "boards": {},
        "timestamp": datetime.now().isoformat(),
    }

    # 1. 获取融资余额 (AKShare)
    margin_data = fetch_margin_by_board(date_str)

    # 2. 确定流通市值
    circ_mv = dict(REFERENCE_DATA["circulating_mv"])
    if circulating_mv_overrides:
        circ_mv.update(circulating_mv_overrides)

    # 3. 计算各板块
    all_margin = 0.0
    all_circ_mv = 0.0

    for board_name in ["创业板", "科创板", "深市主板", "沪市主板"]:
        margin = margin_data[board_name]
        mv = circ_mv.get(board_name)

        if mv and mv > 0:
            ratio = margin / (mv * 1e8) * 100  # margin 是元, mv 是亿元
            is_risk = ratio > threshold
            result["boards"][board_name] = {
                "margin_yi": round(margin / 1e8, 2),
                "circ_mv_yi": mv,
                "ratio_pct": round(ratio, 2),
                "is_risk": is_risk,
            }
            all_margin += margin
            all_circ_mv += mv * 1e8
        else:
            result["boards"][board_name] = {
                "margin_yi": round(margin / 1e8, 2),
                "circ_mv_yi": None,
                "ratio_pct": None,
                "is_risk": None,
            }
            all_margin += margin

    # 4. 沪深两市合计
    total_circ_mv = circ_mv.get("沪深两市")
    if total_circ_mv and total_circ_mv > 0:
        total_ratio = all_margin / (total_circ_mv * 1e8) * 100
        result["total"] = {
            "margin_yi": round(all_margin / 1e8, 2),
            "circ_mv_yi": total_circ_mv,
            "ratio_pct": round(total_ratio, 2),
            "is_risk": total_ratio > threshold,
        }
    elif all_circ_mv > 0:
        total_ratio = all_margin / all_circ_mv * 100
        result["total"] = {
            "margin_yi": round(all_margin / 1e8, 2),
            "circ_mv_yi": round(all_circ_mv / 1e8, 2),
            "ratio_pct": round(total_ratio, 2),
            "is_risk": total_ratio > threshold,
        }

    return result


# ---- 输出 ----
def format_report(result: dict) -> str:
    """格式化输出报告。"""
    lines = []
    lines.append("=" * 60)
    lines.append("市场杠杆风险监测报告")
    lines.append(f"数据日期: {result['date']}")
    lines.append(f"风险阈值: 融资余额/流通市值 > {result['threshold']}%")
    lines.append("=" * 60)

    lines.append("")
    lines.append(f"{'板块':<10} {'融资余额(亿)':>12} {'流通市值(亿)':>14} {'占比':>8} {'状态':>6}")
    lines.append("-" * 60)

    for board_name in ["创业板", "科创板", "深市主板", "沪市主板"]:
        b = result["boards"].get(board_name, {})
        margin_str = f"{b.get('margin_yi', 0):,.2f}"
        circ_str = f"{b['circ_mv_yi']:,.2f}" if b.get("circ_mv_yi") else "N/A"
        ratio_str = f"{b['ratio_pct']:.2f}%" if b.get("ratio_pct") is not None else "N/A"
        is_risk = b.get("is_risk")
        status = "🔴 风险" if is_risk else ("🟢 安全" if is_risk is False else "❓")
        lines.append(f"{board_name:<10} {margin_str:>12} {circ_str:>14} {ratio_str:>8} {status:>6}")

    lines.append("-" * 60)
    t = result.get("total", {})
    if t:
        margin_str = f"{t.get('margin_yi', 0):,.2f}"
        circ_str = f"{t['circ_mv_yi']:,.2f}" if t.get("circ_mv_yi") else "N/A"
        ratio_str = f"{t['ratio_pct']:.2f}%" if t.get("ratio_pct") is not None else "N/A"
        is_risk = t.get("is_risk")
        status = "🔴 风险" if is_risk else ("🟢 安全" if is_risk is False else "❓")
        lines.append(f"{'沪深两市':<10} {margin_str:>12} {circ_str:>14} {ratio_str:>8} {status:>6}")

    # 风险提示
    risk_boards = [n for n, b in result["boards"].items() if b.get("is_risk") is True]
    if result.get("total", {}).get("is_risk"):
        risk_boards.append("沪深两市")
    if risk_boards:
        lines.append(f"\n⚠️  风险警示: {', '.join(risk_boards)} 超过 {result['threshold']}% 阈值")
    else:
        lines.append(f"\n✅ 各板块均在 {result['threshold']}% 以下，杠杆风险可控。")

    # 数据来源说明
    lines.append(f"\n📋 融资余额: AKShare (交易所数据)")
    lines.append(f"   流通市值: 交易所官方统计 (参考数据日期: {REFERENCE_DATA['date']})")
    lines.append(f"   注意: 流通市值应定期更新, 可用 --xxx-mv 参数传入最新值")

    return "\n".join(lines)


# ---- 入口 ----
def main():
    parser = argparse.ArgumentParser(description="市场杠杆风险监测")
    parser.add_argument("--date", help="融资余额日期 YYYYMMDD")
    parser.add_argument("--threshold", type=float, default=RISK_THRESHOLD,
                        help=f"风险阈值%%(默认{RISK_THRESHOLD})")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--cyb-mv", type=float, help="创业板流通市值(亿元)")
    parser.add_argument("--kcb-mv", type=float, help="科创板流通市值(亿元)")
    parser.add_argument("--sz-main-mv", type=float, help="深市主板流通市值(亿元)")
    parser.add_argument("--sh-main-mv", type=float, help="沪市主板流通市值(亿元)")
    parser.add_argument("--total-mv", type=float, help="沪深两市总流通市值(亿元)")
    args = parser.parse_args()

    overrides = {}
    if args.cyb_mv:
        overrides["创业板"] = args.cyb_mv
    if args.kcb_mv:
        overrides["科创板"] = args.kcb_mv
    if args.sz_main_mv:
        overrides["深市主板"] = args.sz_main_mv
    if args.sh_main_mv:
        overrides["沪市主板"] = args.sh_main_mv
    if args.total_mv:
        overrides["沪深两市"] = args.total_mv

    result = analyze(
        date_str=args.date,
        threshold=args.threshold,
        circulating_mv_overrides=overrides if overrides else None,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
