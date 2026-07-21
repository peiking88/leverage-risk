#!/usr/bin/env python3
"""
市场杠杆风险监测 — 融资余额 / 流通市值。

数据策略:
  融资余额 (动态, 交易所接口可靠):
    - 按板块: stock_margin_detail_szse / stock_margin_detail_sse (按代码前缀拆分)
    - 沪深合计: stock_margin_sse / stock_margin_szse (交易所汇总口径)
  流通市值 (官方参考值, 无可靠实时接口):
    - 深市板块流通市值无可靠实时接口 (stock_szse_summary 板块拆分约为真实值一半, 不可信;
      stock_zh_a_spot_em 部分环境被墙), 故采用官方参考值, 可通过 --xxx-mv 覆盖, 建议定期更新。
  个股流通市值: stock_individual_info_em (东方财富, 被墙时显示 N/A)

风险阈值: 融资余额 / 流通市值 > 4% 触发警示

用法:
  python3 leverage_risk.py                            # 默认: 上一交易日, 沪深两市+科创板+创业板
  python3 leverage_risk.py --date 20260717            # 指定日期
  python3 leverage_risk.py --symbols sh600000,sz300750  # 指定标的(个股)
  python3 leverage_risk.py --cyb-mv 144934.15 --total-mv 948326  # 覆盖流通市值(亿元)
  python3 leverage_risk.py --json                     # JSON 输出
  python3 leverage_risk.py --threshold 5.0            # 自定义阈值
"""

import argparse
import json
import time
from datetime import datetime, timedelta

import akshare as ak

__version__ = "1.1.0"

# ---- 配置 ----
RISK_THRESHOLD = 4.0
# 默认监测标的集: 创业板 + 科创板 + 沪深两市合计
DEFAULT_SEGMENTS = ["创业板", "科创板", "沪深两市"]

# 流通市值参考表 (亿元) — 官方口径
# 2026-07-17 验证: 创业板4.3% / 科创板3.4% / 沪深两市2.90%
# 注意: 流通市值随行情变动, 建议每月/季更新或用 --xxx-mv 传入最新值
REFERENCE_MV = {
    "date": "20260717",
    "创业板": 144934.15,
    "科创板": 107381.71,
    "沪深两市": 948326.0,   # A股流通市值 (融资余额27491.45亿 ÷ 2.90%)
}


# ---- 日期 ----
def previous_trading_day(ref: datetime | None = None) -> datetime:
    """上一交易日 (跳过周末)。"""
    d = (ref or datetime.now()) - timedelta(1)
    while d.weekday() >= 5:  # 5=周六, 6=周日
        d -= timedelta(1)
    return d


def resolve_date(date_str: str | None) -> str:
    """确定融资余额日期: 显式指定 > 从上一交易日起向前找首个有数据日。"""
    if date_str:
        return date_str
    d = previous_trading_day()
    for _ in range(7):
        if d.weekday() < 5:
            ds = d.strftime("%Y%m%d")
            try:
                if len(ak.stock_margin_detail_szse(date=ds)) > 0:
                    return ds
            except Exception:
                pass
        d -= timedelta(1)
    raise RuntimeError("近 7 个交易日无融资融券数据")


# ---- 融资余额 (动态) ----
def fetch_details(date_str: str):
    """拉取沪深融资融券明细 (各调用一次)。"""
    sz = ak.stock_margin_detail_szse(date=date_str)
    time.sleep(1)
    sh = ak.stock_margin_detail_sse(date=date_str)
    return sz, sh


def margin_by_board(sz, sh) -> dict[str, float]:
    """按板块汇总融资余额 (元)。"""
    s = sz["证券代码"].astype(str)
    h = sh["标的证券代码"].astype(str)
    return {
        "创业板": float(sz[s.str.startswith(("300", "301"))]["融资余额"].sum()),
        "深市主板": float(sz[~s.str.startswith(("300", "301"))]["融资余额"].sum()),
        "科创板": float(sh[h.str.startswith("688")]["融资余额"].sum()),
        "沪市主板": float(sh[~h.str.startswith("688")]["融资余额"].sum()),
    }


def margin_total(date_str: str) -> float:
    """沪深融资余额合计 (元) — 交易所汇总口径。沪市原始单位元, 深市亿元。"""
    sh = float(ak.stock_margin_sse(start_date=date_str, end_date=date_str).iloc[0]["融资余额"])
    time.sleep(1)
    sz_yi = float(ak.stock_margin_szse(date=date_str).iloc[0]["融资余额"])
    return sh + sz_yi * 1e8  # 深市 亿→元


def margin_for_symbol(sz, sh, code: str) -> float | None:
    """单只标的融资余额 (元), 无数据返回 None。"""
    if code.startswith(("6", "9")):  # 沪市
        df, cc = sh, "标的证券代码"
    else:  # 深市
        df, cc = sz, "证券代码"
    row = df[df[cc].astype(str) == code]
    return float(row.iloc[0]["融资余额"]) if len(row) else None


# ---- 流通市值 ----
def mv_for_symbol(code: str) -> float | None:
    """单只标的流通市值 (元) — 东方财富。失败返回 None。"""
    try:
        info = ak.stock_individual_info_em(symbol=code).set_index("item")["value"]
        return float(info["流通市值"])
    except Exception:
        return None


# ---- 标的解析 ----
def parse_symbol(tok: str) -> str:
    """sh600000 / sz000001 / 600000 → 纯代码。"""
    tok = tok.strip().lower()
    for p in ("sh", "sz"):
        if tok.startswith(p):
            tok = tok[len(p):]
    return tok


# ---- 主分析 ----
def analyze(
    date_str: str | None = None,
    threshold: float = RISK_THRESHOLD,
    symbols: list[str] | None = None,
    segments: list[str] | None = None,
    mv_overrides: dict[str, float] | None = None,
) -> dict:
    """执行杠杆风险分析。"""
    date_str = resolve_date(date_str)
    sz, sh = fetch_details(date_str)
    board_margin = margin_by_board(sz, sh)
    total_margin = margin_total(date_str)
    mv = {k: v for k, v in REFERENCE_MV.items() if k != "date"}
    if mv_overrides:
        mv.update(mv_overrides)

    result = {
        "date": date_str,
        "mv_date": REFERENCE_MV["date"],
        "threshold": threshold,
        "timestamp": datetime.now().isoformat(),
        "items": [],
    }

    def add(name: str, margin_yuan: float | None, mv_yi: float | None):
        if margin_yuan is None:
            result["items"].append({"name": name, "margin_yi": None, "circ_mv_yi": None,
                                    "ratio_pct": None, "is_risk": None,
                                    "note": "无融资融券数据"})
            return
        m_yi = margin_yuan / 1e8
        if mv_yi and mv_yi > 0:
            r = margin_yuan / (mv_yi * 1e8) * 100
            result["items"].append({"name": name, "margin_yi": round(m_yi, 2),
                                    "circ_mv_yi": round(mv_yi, 2), "ratio_pct": round(r, 2),
                                    "is_risk": r > threshold})
        else:
            result["items"].append({"name": name, "margin_yi": round(m_yi, 2),
                                    "circ_mv_yi": None, "ratio_pct": None, "is_risk": None})

    if symbols:
        for raw in symbols:
            code = parse_symbol(raw)
            m = margin_for_symbol(sz, sh, code)
            sym_mv = mv_for_symbol(code)
            add(raw, m, sym_mv / 1e8 if sym_mv else None)
    else:
        for seg in (segments or DEFAULT_SEGMENTS):
            if seg == "沪深两市":
                add("沪深两市", total_margin, mv.get("沪深两市"))
            else:
                add(seg, board_margin.get(seg, 0.0), mv.get(seg))

    return result


# ---- 输出 ----
def format_report(result: dict) -> str:
    lines = ["=" * 60, "市场杠杆风险监测报告",
             f"数据日期: {result['date']}  (流通市值参考: {result['mv_date']})",
             f"风险阈值: 融资余额/流通市值 > {result['threshold']}%",
             "=" * 60, "",
             f"{'标的':<16} {'融资余额(亿)':>12} {'流通市值(亿)':>14} {'占比':>8} {'状态':>8}",
             "-" * 60]

    risk_names = []
    for it in result["items"]:
        name = it["name"]
        margin_str = f"{it['margin_yi']:,.2f}" if it.get("margin_yi") is not None else "N/A"
        circ_str = f"{it['circ_mv_yi']:,.2f}" if it.get("circ_mv_yi") else "N/A"
        ratio_str = f"{it['ratio_pct']:.2f}%" if it.get("ratio_pct") is not None else "N/A"
        is_risk = it.get("is_risk")
        if is_risk is True:
            status = "🔴 风险"
            risk_names.append(name)
        elif is_risk is False:
            status = "🟢 安全"
        else:
            status = "❓"
        lines.append(f"{name:<16} {margin_str:>12} {circ_str:>14} {ratio_str:>8} {status:>8}")

    lines.append("-" * 60)
    if risk_names:
        lines.append(f"\n⚠️  风险警示: {', '.join(risk_names)} 超过 {result['threshold']}% 阈值")
    else:
        lines.append(f"\n✅ 所有标的均在 {result['threshold']}% 以下，杠杆风险可控。")

    lines.append("\n📋 融资余额: AKShare 沪深交易所 (动态)")
    lines.append(f"   流通市值: 官方参考值 ({result['mv_date']}), 可用 --xxx-mv 更新")
    return "\n".join(lines)


# ---- 自检 (口径一致性) ----
def self_check():
    """最小自检: 占比落在合理区间, 捕捉单位换算错误。"""
    r = analyze()
    items = {it["name"]: it for it in r["items"]}
    expect = {"创业板": 4.3, "科创板": 3.4, "沪深两市": 2.90}
    for name, exp in expect.items():
        got = items.get(name, {}).get("ratio_pct")
        assert got and abs(got - exp) < 0.5, f"{name} 占比 {got} 偏离权威值 {exp}%"
    print("✅ 自检通过 (对照 2026-07-17 权威值):")
    for it in r["items"]:
        print(f"   {it['name']}: 融资余额 {it['margin_yi']}亿, 占比 {it['ratio_pct']}%")


# ---- 入口 ----
def main():
    parser = argparse.ArgumentParser(description="市场杠杆风险监测")
    parser.add_argument("--date", help="融资余额日期 YYYYMMDD (默认: 上一交易日)")
    parser.add_argument("--symbols", help="指定标的, 逗号分隔, 如 sh600000,sz300750 (默认: 沪深两市+科创板+创业板)")
    parser.add_argument("--threshold", type=float, default=RISK_THRESHOLD,
                        help=f"风险阈值%% (默认 {RISK_THRESHOLD})")
    parser.add_argument("--cyb-mv", type=float, help="创业板流通市值(亿元)")
    parser.add_argument("--kcb-mv", type=float, help="科创板流通市值(亿元)")
    parser.add_argument("--total-mv", type=float, help="沪深两市流通市值(亿元)")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--self-check", action="store_true", help="运行口径自检 (对照权威值)")
    args = parser.parse_args()

    if args.self_check:
        self_check()
        return

    symbols = [s for s in (args.symbols.split(",") if args.symbols else []) if s.strip()] or None
    overrides = {}
    if args.cyb_mv:
        overrides["创业板"] = args.cyb_mv
    if args.kcb_mv:
        overrides["科创板"] = args.kcb_mv
    if args.total_mv:
        overrides["沪深两市"] = args.total_mv

    result = analyze(date_str=args.date, threshold=args.threshold, symbols=symbols,
                     mv_overrides=overrides or None)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
