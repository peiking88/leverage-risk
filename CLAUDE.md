# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**leverage-risk** — 市场杠杆风险监测技能。计算 A 股主板、创业板、科创板融资余额占各自流通市值的比例，>4% 触发风险警示；同步计算交易拥挤度（成交额前5%个股占比，>=45% 风险）。

## Commands

```bash
# 默认: 上一交易日, 沪深两市 + 科创板 + 创业板
python3 scripts/leverage_risk.py

# 指定日期 (YYYYMMDD)
python3 scripts/leverage_risk.py --date 20260717

# 指定标的(个股), 逗号分隔; 支持 sh/sz 前缀或纯代码
python3 scripts/leverage_risk.py --symbols sh600000,sz300750

# 覆盖流通市值(亿元)
python3 scripts/leverage_risk.py --cyb-mv 144934.15 --total-mv 948326

# JSON 输出 / 自定义阈值 / 自检
python3 scripts/leverage_risk.py --json
python3 scripts/leverage_risk.py --threshold 5.0
python3 scripts/leverage_risk.py --self-check
```

## Architecture

### 数据策略

```
融资余额 (动态, 交易所接口可靠)
  ├── stock_margin_detail_szse/sse(date) → 按代码前缀拆 4 板块
  └── stock_margin_sse/szse(date)        → 沪深合计(汇总口径)
流通市值 (交易所官方接口实时获取, 失败回退 REFERENCE_MV)
  ├── 沪: stock_sse_summary → 主板/科创板/合计 (亿元, T 日收盘即有)
  └── 深: stock_szse_summary(date=) → 创业板/合计 (元; 必须带 date, 无参返回异常子集)
  ↳ 回退: REFERENCE_MV (创业板 144934.15 / 科创板 107381.71 / 沪深两市 948326.0, 亿元, 2026-07-17)
  交易拥挤度 (全市场模式)
    └── stock_zh_a_spot (新浪) → 成交额排名前5%个股总成交额 / 全市场成交额
        (新浪接口, 获取失败跳过, 不影响杠杆主体)
        ↓
  融资余额(元) / 流通市值(亿元×1e8) × 100 = 占比(%)
        ↓
  占比 > threshold → 风险警示; 拥挤度 >= 45% → 风险警示
```

### 关键设计决策

- **融资余额 + 流通市值均动态**：融资余额 `stock_margin_*` 交易所接口 T+1 可靠；流通市值 `stock_sse_summary`(沪) + `stock_szse_summary(date=)`(深) 交易所官方接口 T 日收盘即有，实时获取，失败回退 `REFERENCE_MV`，可 `--xxx-mv` 覆盖。
- **深市流通市值必须带 date**：`stock_szse_summary()` 无参返回异常子集（创业板 69,004 亿，约为真实值一半）；带 `date=` 才返回完整口径（150,924 亿，与官方一致）。详见下文「数据源可靠性」。
- **单位不对称（坑）**：`stock_margin_sse` 融资余额为**元**，`stock_margin_szse` 为**亿元**（×1e8 统一）。
- **沪深合计用汇总口径**：`沪深两市` 行融资余额取 `stock_margin_sse/szse` 汇总（27,491 亿），略大于 4 板块明细之和（明细仅含两融标的），属正常。
- **板块分类**：按代码前缀。沪市主板取 600/601/603/605（排除 688），深市主板排除 300/301。
- **默认标的集**：创业板 + 科创板 + 沪深两市。`--symbols` 切换个股模式（融资余额取明细，流通市值取 `stock_zh_a_daily` 收盘价×总股本(新浪)，无东方财富依赖）。
- **交易拥挤度**：仅全市场模式计算。`stock_zh_a_spot`（新浪）取全市场逐股成交额，排序后前5%成交额占比 >= 45% 标记风险；接口不稳时返回 `None` 跳过，不阻塞杠杆分析。阈值可用 `--crowding-threshold` 调整。

### 数据源可靠性速查

| 接口 | 用途 | 可靠性 | 备注 |
|------|------|--------|------|
| `stock_margin_detail_szse/sse` | 融资余额明细（按代码前缀拆板块） | ✅ 可靠 | 单位：元 |
| `stock_margin_sse` | 沪市融资余额汇总 | ✅ 可靠 | 单位：**元** |
| `stock_margin_szse` | 深市融资余额汇总 | ✅ 可靠 | 单位：**亿元**（与沪市不对称） |
| `stock_sse_summary` | 沪市流通市值（主板/科创板/合计） | ✅ 可靠 | T 日收盘即有；单位亿元 |
| `stock_szse_summary(date=)` | 深市流通市值（主板/创业板/合计） | ✅ **带 date 可靠** | **必须带 date 参数**；单位元。无参调用返回异常子集（创业板 69,004 亿，约为真实值一半） |
| `stock_zh_a_spot` | 全市场逐股成交额（交易拥挤度） | ✅ 可靠 | 新浪全市场行情，含"成交额"列；失败时跳过拥挤度，不阻塞杠杆分析 |
| `stock_zh_a_daily` | 个股最新收盘价 | ✅ 可靠 | 个股流通市值取价用；末行=最近交易日收盘，时点与融资余额(T+1)匹配 |

### 经验教训

- **声明参考数据"错误"前，先用权威数据交叉验证数据源本身**。v1.1 初版曾据 `stock_szse_summary` 断言"创业板流通市值 144,934 亿是错的（实际 69,004 亿）"——实际恰好相反，是接口数据失真。教训：当计算结果与既有参考冲突时，先怀疑新数据源，用官方/多源交叉验证后再下结论。
- **同一接口不同调用方式结论相反**：`stock_szse_summary()` 无参返回创业板 69,004 亿（异常子集），`stock_szse_summary(date="20260721")` 返回 150,924 亿（与官方 144,934 亿一致，可信）。v1.2 修正：深市流通市值改用带 date 调用实时获取，推翻 v1.1 "接口不可信" 结论。教训：判定接口"不可信"前，先排查调用参数（带不带 date、单位）。
- **交易所服务器接口（沪深官网）比第三方聚合（东方财富）更稳定**，优先选用；第三方接口受网络/反爬影响大。
- **同一接口按股票代码返回不同列名**：akshare 1.18.60 `stock_financial_report_sina` 资产负债表，沪市主板返回 `"股本"`，创业板/科创板返回 `"实收资本(或股本)"`。`mv_for_symbol()` 现已兼容两列名。教训：依赖第三方接口列名时，优先查 `col in columns` 而非硬编码，避免按股票子集取列。

### 文件结构

本项目即技能本身。顶层 = git 源码（**提交对象**）；`.claude/skills/leverage-risk/` = 技能安装版（**运行用**，被 `.gitignore` 忽略，与顶层保持内容同步）：

```
hot-skills/financial/leverage-risk/           # 项目根 = git 仓库
├── SKILL.md / CLAUDE.md / scripts/            # ← 源码（git 追踪，提交对象）
├── .claude/skills/leverage-risk/             # ← 技能安装版（运行用，被忽略）
│   ├── SKILL.md
│   ├── CLAUDE.md
│   └── scripts/leverage_risk.py
└── .gitignore                                 # 忽略 .claude/ 与 summary.md
```

维护约定：顶层文件是 git 权威源，修改后同步复制到 `.claude/skills/leverage-risk/` 即可（安装版不提交）。

## Dependencies

```
akshare >= 1.0    # 唯一外部数据源
pandas >= 1.0     # 随 akshare 引入
```

## Validation (2026-07-17 官方口径)

| 板块 | 脚本融资余额(亿) | 官方(亿) | 流通市值(亿) | 脚本占比 | 官方占比 |
|------|---|---|---|---|---|
| 创业板 | 6,177.41 | 6,188.66 | 144,934.15 | 4.26% | 4.3% |
| 科创板 | 3,636.44 | 3,648.79 | 107,381.71 | 3.39% | 3.4% |
| 沪深两市 | 27,491.45 | — | 948,326.00 | 2.90% | 2.90% |

占比微小差异（<0.05%）源于 AKShare 融资余额较官方统计低约 0.18%（统计时点差），流通市值取值一致。`--self-check` 内置此对照。

> v1.2 起，流通市值实时获取（交易所官方接口）。上表为 2026-07-17 基线；运行时流通市值取最新交易日（如 2026-07-21：创业板 150,924 / 科创板 114,235 / 沪深两市 973,277 亿），占比随分母实时变动（创业板当日 3.98%）。

## Common Modifications

- **流通市值**：默认实时获取（交易所官方接口）；回退/手动覆盖用 `REFERENCE_MV` 或 `--cyb-mv/--kcb-mv/--total-mv`
- **调整风险阈值**：`RISK_THRESHOLD` 或 `--threshold`
- **新增板块**：在 `margin_by_board` 添加前缀规则，并补充 `REFERENCE_MV`
