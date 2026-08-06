#!/usr/bin/env python3
"""
市场杠杆风险监测 — 融资余额 / 流通市值 + 交易拥挤度。

数据策略:
  融资余额 (动态, 交易所接口可靠):
    - 按板块: stock_margin_detail_szse / stock_margin_detail_sse (按代码前缀拆分)
    - 沪深合计: stock_margin_sse / stock_margin_szse (交易所汇总口径)
  流通市值 (交易所官方接口实时获取, 失败回退 REFERENCE_MV):
    - 沪: stock_sse_summary (主板/科创板/合计, 单位亿元)
    - 深: stock_szse_summary(date=) (创业板/合计, 单位元; 必须带 date, 无参返回异常子集)
    - T 日收盘即有 (比融资余额 T+1 更新), 失败回退 REFERENCE_MV, 可用 --xxx-mv 覆盖。
  个股流通市值: AKShare 新浪数据源 — 最新收盘价 (stock_zh_a_daily) × 总股本 (资产负债表实收资本) ≈ 总市值
    (spot 实时价开市前为0, 故用日线最近收盘价; 流通占比高时总市值≈流通市值; 无东方财富依赖)
  交易拥挤度: 新浪全市场行情 (stock_zh_a_spot) — 成交额排名前5%个股总成交额 / 全市场成交额
    (ponytail: 新浪接口开市前价格为0但不影响成交额列, 获取失败时跳过拥挤度, 不影响杠杆主体分析)

风险阈值: 融资余额/流通市值 > 4% 触发警示; 交易拥挤度 >= 45% 触发警示

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
import sys
import time
from datetime import datetime, timedelta

import akshare as ak

__version__ = "1.5.0"

# ---- 配置 ----
RISK_THRESHOLD = 4.0
CROWDING_THRESHOLD = 45.0  # 成交额前5%个股占比 >= 45% 触发拥挤度警示
CROWDING_TOP_PCT = 0.05    # 排名前列个股比例
# 默认监测标的集: 创业板 + 科创板 + 沪深两市合计
DEFAULT_SEGMENTS = ["创业板", "科创板", "沪深两市"]

# 流通市值参考表 (亿元) — 官方口径, 作为实时接口失败时的回退值
# 正常 fetch_live_mv() 从沪深交易所官方接口实时获取; 此表仅在接口异常时兜底。
# 2026-07-17 验证基线: 创业板4.3% / 科创板3.4% / 沪深两市2.90%
# 注意: 流通市值随行情变动, 建议每季更新或用 --xxx-mv 传入最新值
REFERENCE_MV = {
    "date": "20260717",
    "创业板": 144934.15,
    "科创板": 107381.71,
    "沪深两市": 948326.0,
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
    """拉取沪深融资融券明细 (各调用一次)。数据未发布时 sse 接口返回空表会内部抛
    ValueError, szse 返回空 DataFrame; 统一检测并转译为友好提示 (T+1 公布)。"""
    msg = (f"日期 {date_str} 融资融券数据未发布 (T+1 公布: 非交易日, 或当日数据次日才更新)。"
           f"建议省略 --date 自动取最近有数据交易日, 或改用更早日期。")
    try:
        sz = ak.stock_margin_detail_szse(date=date_str)
        time.sleep(1)
        sh = ak.stock_margin_detail_sse(date=date_str)
    except ValueError as e:
        raise ValueError(msg) from e
    if sz.empty or sh.empty:
        raise ValueError(msg)
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
def mv_for_symbol(code: str) -> tuple[float | None, str]:
    """单只标的流通市值 (元) 及来源。

    新浪收盘价(stock_zh_a_daily) × 总股本(资产负债表实收资本) ≈ 总市值。
    流通占比高时总市值≈流通市值; 无东方财富依赖。
    ponytail: 实收资本单位为元, A股面值1元故数值=股本(股); 用总股本算的是总市值,
              次新股有限售股时偏高; stock_zh_a_spot 全市场价格常返0故改用日线。
    """
    prefix = "sh" if code.startswith(("5", "6", "9")) else "sz"
    try:
        daily = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="")
        price = float(daily.iloc[-1]["close"])
        if price <= 0:
            return None, "N/A(收盘价为0)"
        bs = ak.stock_financial_report_sina(stock=code, symbol="资产负债表")
        # akshare 列名不一致: 沪市主板返回 "股本", 创业板/科创板返回 "实收资本(或股本)"
        row = bs.iloc[0]
        shares = None
        for col in ("实收资本(或股本)", "股本"):
            if col in bs.columns:
                v = row[col]
                if v is not None and not (isinstance(v, float) and v != v) and float(v) > 0:  # 非NaN且>0
                    shares = float(v)
                    break
        if shares is None:
            return None, "N/A(股本列缺失或无效)"
        return price * shares, "新浪收盘价×总股本(总市值近似)"
    except Exception as e:
        return None, f"N/A({type(e).__name__})"


def fetch_live_mv() -> dict | None:
    """实时获取板块流通市值(亿元) — 沪深交易所官方接口, T 日收盘即有 (比融资余额 T+1 更新)。
    沪: stock_sse_summary (单位亿元, 最新交易日, 含主板/科创板/股票合计)
    深: stock_szse_summary(date=) (单位元, 必须带 date; 无参返回异常子集, 勿用)
    返回 {板块: 亿元, 'date': 数据日期}; 任一接口失败返回 None (调用方回退 REFERENCE_MV)。"""
    try:
        sse = ak.stock_sse_summary().set_index("项目")
        sh_total = float(sse.loc["流通市值", "股票"])           # 亿元
        kcb = float(sse.loc["流通市值", "科创板"])
        mv_date = str(int(float(sse.loc["报告时间", "股票"])))  # 兼容 "20260721" / 20260721.0
        time.sleep(1)
        sz = ak.stock_szse_summary(date=mv_date).set_index("证券类别")["流通市值"]  # 元
        cyb = float(sz["创业板A股"]) / 1e8
        sz_total = float(sz["股票"]) / 1e8
        return {"创业板": cyb, "科创板": kcb,
                "沪深两市": sh_total + sz_total, "date": mv_date}
    except Exception:
        return None


# ---- 交易拥挤度 ----
def _board_of(code: str) -> str:
    """按代码前缀分类板块 (用于拥挤度分组)。"""
    c = code[2:] if code[:2] in ("sh", "sz", "bj") else code
    if c.startswith(("300", "301")):
        return "创业板"
    if c.startswith("688"):
        return "科创板"
    return "主板"  # 沪深两市 = 全市场, 此处仅作兜底分类


def fetch_crowding() -> dict | None:
    """交易拥挤度按板块: 各板块成交额排名前5%个股总成交额 / 该板块成交额 (%)。

    数据来源: 新浪全市场行情 (stock_zh_a_spot), "成交额" 单位元。
    返回 {板块: {ratio, top_n, total_n, top_amount_yi, total_amount_yi}}，
    含 "创业板"、"科创板"、"沪深两市"(全市场); 失败返回 None。
    """
    try:
        spot = ak.stock_zh_a_spot()
        df = spot[["代码", "成交额"]].dropna(subset=["成交额"])
        df = df[df["成交额"] > 0].copy()
        if df.empty:
            return None
        df["board"] = df["代码"].astype(str).map(_board_of)

        def _calc(amt):
            s = amt.sort_values(ascending=False)
            total = float(s.sum())
            n = len(s)
            top_n = max(1, int(n * CROWDING_TOP_PCT + 0.5))  # 四舍五入取整
            top_sum = float(s.iloc[:top_n].sum())
            return {
                "ratio": round(top_sum / total * 100, 2) if total > 0 else 0.0,
                "top_n": top_n,
                "total_n": n,
                "top_amount_yi": round(top_sum / 1e8, 2),
                "total_amount_yi": round(total / 1e8, 2),
            }

        out = {}
        for board in ("创业板", "科创板"):
            sub = df[df["board"] == board]["成交额"]
            if not sub.empty:
                out[board] = _calc(sub)
        out["沪深两市"] = _calc(df["成交额"])  # 沪深两市 = 全市场口径
        return out
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
    crowding_threshold: float = CROWDING_THRESHOLD,
) -> dict:
    """执行杠杆风险分析。"""
    date_str = resolve_date(date_str)
    sz, sh = fetch_details(date_str)
    board_margin = margin_by_board(sz, sh)
    total_margin = margin_total(date_str)
    live = fetch_live_mv()
    if live:
        mv = {k: live[k] for k in ("创业板", "科创板", "沪深两市")}
        mv_date = live["date"]
        mv_source = "交易所官方接口(实时)"
    else:
        mv = {k: v for k, v in REFERENCE_MV.items() if k != "date"}
        mv_date = REFERENCE_MV["date"]
        mv_source = "官方参考值(静态, 接口失败回退)"
    if mv_overrides:
        mv.update(mv_overrides)
        mv_source = "手动覆盖(--xxx-mv)"

    result = {
        "date": date_str,
        "mv_date": mv_date,
        "mv_source": mv_source,
        "threshold": threshold,
        "crowding_threshold": crowding_threshold,
        "timestamp": datetime.now().isoformat(),
        "items": [],
    }

    def add(name: str, margin_yuan: float | None, mv_yi: float | None, mv_source: str = "",
            crowding_ratio: float | None = None):
        if margin_yuan is None:
            result["items"].append({"name": name, "margin_yi": None, "circ_mv_yi": None,
                                    "ratio_pct": None, "is_risk": None,
                                    "note": "无融资融券数据"})
            return
        m_yi = margin_yuan / 1e8
        if mv_yi and mv_yi > 0:
            r = margin_yuan / (mv_yi * 1e8) * 100
            item = {"name": name, "margin_yi": round(m_yi, 2), "circ_mv_yi": round(mv_yi, 2),
                    "ratio_pct": round(r, 2), "is_risk": r > threshold, "mv_source": mv_source}
        else:
            item = {"name": name, "margin_yi": round(m_yi, 2), "circ_mv_yi": None,
                    "ratio_pct": None, "is_risk": None, "mv_source": mv_source}
        # 交易拥挤度 (按板块)
        if crowding_ratio is not None:
            item["crowding_ratio"] = crowding_ratio
            item["crowding_risk"] = crowding_ratio >= crowding_threshold
        result["items"].append(item)

    # 交易拥挤度 (仅全市场模式; 获取失败返回 None, 不影响杠杆分析)
    crowding = fetch_crowding() if not symbols else None

    if symbols:
        for raw in symbols:
            code = parse_symbol(raw)
            m = margin_for_symbol(sz, sh, code)
            sym_mv, sym_src = mv_for_symbol(code)
            add(raw, m, sym_mv / 1e8 if sym_mv else None, mv_source=sym_src)
    else:
        for seg in (segments or DEFAULT_SEGMENTS):
            cr = crowding.get(seg, {}).get("ratio") if crowding else None
            if seg == "沪深两市":
                add("沪深两市", total_margin, mv.get("沪深两市"), crowding_ratio=cr)
            else:
                add(seg, board_margin.get(seg, 0.0), mv.get(seg), crowding_ratio=cr)

    result["crowding"] = crowding  # {板块: {...}} 或 None

    return result


# ---- 输出 ----
def format_report(result: dict) -> str:
    cr_threshold = result.get("crowding_threshold", CROWDING_THRESHOLD)
    lines = ["=" * 70, "市场杠杆风险监测报告",
             f"数据日期: {result['date']}  (流通市值: {result['mv_date']}, {result.get('mv_source', '')})",
             f"风险阈值: 融资余额/流通市值 > {result['threshold']}%   "
             f"交易拥挤度(前{CROWDING_TOP_PCT:.0%}个股占比) >= {cr_threshold}%",
             "=" * 70, "",
             f"{'标的':<12} {'融资余额(亿)':>12} {'流通市值(亿)':>12} {'占比':>7} {'拥挤度':>7} {'状态':>6}",
             "-" * 70]

    risk_names = []
    for it in result["items"]:
        name = it["name"]
        margin_str = f"{it['margin_yi']:,.2f}" if it.get("margin_yi") is not None else "N/A"
        circ_str = f"{it['circ_mv_yi']:,.2f}" if it.get("circ_mv_yi") else "N/A"
        ratio_str = f"{it['ratio_pct']:.2f}%" if it.get("ratio_pct") is not None else "N/A"
        # 拥挤度列
        cr_ratio = it.get("crowding_ratio")
        if cr_ratio is not None:
            cr_flag = "🔴" if it.get("crowding_risk") else "🟢"
            crowding_str = f"{cr_ratio:.1f}%{cr_flag}"
        else:
            crowding_str = "—"
        is_risk = it.get("is_risk")
        if is_risk is True:
            status = "🔴 风险"
            risk_names.append(name)
        elif is_risk is False:
            status = "🟢 安全"
        else:
            status = "❓"
        lines.append(f"{name:<12} {margin_str:>12} {circ_str:>12} {ratio_str:>7} {crowding_str:>7} {status:>6}")
        # 拥挤度风险记入警示
        if it.get("crowding_risk"):
            risk_names.append(f"{name}拥挤度{cr_ratio:.1f}%")

    lines.append("-" * 70)

    # 拥挤度明细 (全市场口径)
    cr = result.get("crowding", {}).get("沪深两市") if result.get("crowding") else None
    if cr:
        lines.append(f"\n📊 拥挤度明细 — 全市场: 前 {cr['top_n']} 股成交额 {cr['top_amount_yi']:,.2f}亿"
                     f" / 全市场 {cr['total_amount_yi']:,.2f}亿 (共 {cr['total_n']} 股)")
    elif not result["items"] or result["items"][0].get("crowding_ratio") is None:
        lines.append("\n交易拥挤度: 未计算 (个股模式, 或新浪行情接口获取失败)")

    if risk_names:
        lines.append(f"\n⚠️  风险警示: {', '.join(risk_names)}")
    else:
        lines.append(f"\n✅ 所有标的均在 {result['threshold']}% 以下，杠杆风险可控。")

    lines.append("\n📋 融资余额: AKShare 沪深交易所 (动态, T+1)")
    lines.append(f"   流通市值: {result.get('mv_source', '')} ({result['mv_date']}), 可用 --xxx-mv 覆盖")
    # 个股流通市值来源说明
    for it in result["items"]:
        if it.get("mv_source") and it["mv_source"] not in ("", "N/A"):
            lines.append(f"   · {it['name']}: 流通市值来源 = {it['mv_source']}")
    return "\n".join(lines)


# ---- 自检 (口径一致性) ----
def self_check():
    """自检: 实时流通市值获取成功 + 占比落在合理区间 + 与参考值同量级 (捕捉单位换算/接口异常)。"""
    r = analyze()
    assert r.get("mv_source", "").startswith("交易所官方"), \
        f"实时流通市值获取失败, 已回退参考值: {r.get('mv_source')}"
    ref = {k: v for k, v in REFERENCE_MV.items() if k != "date"}
    for it in r["items"]:
        name, ratio = it["name"], it.get("ratio_pct")
        assert ratio and 0 < ratio < 10, f"{name} 占比 {ratio} 越界 [0,10)%"
        mv = it.get("circ_mv_yi")
        if mv and name in ref:
            dev = abs(mv - ref[name]) / ref[name] * 100
            assert dev < 15, f"{name} 实时流通市值 {mv:,.0f}亿 偏离参考值 {ref[name]:,.0f}亿 {dev:.1f}% (接口异常?)"
    print("✅ 自检通过:")
    print(f"   流通市值来源: {r['mv_source']} ({r['mv_date']})")
    for it in r["items"]:
        print(f"   {it['name']}: 融资余额 {it['margin_yi']}亿 / 流通市值 {it['circ_mv_yi']:,.0f}亿 = {it['ratio_pct']}%")


# ---- 入口 ----
def main():
    parser = argparse.ArgumentParser(description="市场杠杆风险监测")
    parser.add_argument("--date", help="融资余额日期 YYYYMMDD (默认: 上一交易日)")
    parser.add_argument("--symbols", help="指定标的, 逗号分隔, 如 sh600000,sz300750 (默认: 沪深两市+科创板+创业板)")
    parser.add_argument("--threshold", type=float, default=RISK_THRESHOLD,
                        help=f"杠杆风险阈值%% (默认 {RISK_THRESHOLD})")
    parser.add_argument("--crowding-threshold", type=float, default=CROWDING_THRESHOLD,
                        help=f"拥挤度阈值%% (成交额前{int(CROWDING_TOP_PCT*100)}%%个股占比, 默认 {CROWDING_THRESHOLD})")
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

    try:
        result = analyze(date_str=args.date, threshold=args.threshold, symbols=symbols,
                         mv_overrides=overrides or None,
                         crowding_threshold=args.crowding_threshold)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
