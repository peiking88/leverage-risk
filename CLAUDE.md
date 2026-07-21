# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**leverage-risk** — 市场杠杆风险监测技能。计算 A 股主板、创业板、科创板融资余额占各自流通市值的比例，>4% 触发风险警示。

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
流通市值 (官方参考值 REFERENCE_MV, 无可靠实时接口)
  └── 创业板 144934.15 / 科创板 107381.71 / 沪深两市 948326.0 (亿元, 2026-07-17)
        ↓
  融资余额(元) / 流通市值(亿元×1e8) × 100 = 占比(%)
        ↓
  占比 > threshold → 风险警示
```

### 关键设计决策

- **融资余额动态、流通市值参考**：融资余额交易所接口可靠，动态取；流通市值无可靠实时接口（见下），用 `REFERENCE_MV` 官方参考值，可通过 `--xxx-mv` 覆盖，建议定期更新。
- **为何流通市值不能动态取（踩坑记录）**：
  - `stock_szse_summary` 深市板块拆分**约为真实值一半**（创业板返回 69,004 亿，实际 144,934 亿），**不可信**。
  - `stock_zh_a_spot_em`（逐股流通市值求和）部分网络环境被墙（RemoteDisconnected）。
  - 故 v1.1 最终采用官方参考值，已用 2026-07-17 权威数据验证通过。
- **单位不对称（坑）**：`stock_margin_sse` 融资余额为**元**，`stock_margin_szse` 为**亿元**（×1e8 统一）。
- **沪深合计用汇总口径**：`沪深两市` 行融资余额取 `stock_margin_sse/szse` 汇总（27,491 亿），略大于 4 板块明细之和（明细仅含两融标的），属正常。
- **板块分类**：按代码前缀。沪市主板取 600/601/603/605（排除 688），深市主板排除 300/301。
- **默认标的集**：创业板 + 科创板 + 沪深两市。`--symbols` 切换个股模式（融资余额取明细，流通市值取 `stock_individual_info_em`，被墙时 N/A）。

### 文件结构

```
~/.claude/skills/leverage-risk/        # 已安装技能（运行入口），与本仓库保持同步
├── SKILL.md / CLAUDE.md
└── scripts/leverage_risk.py
hot-skills/financial/leverage-risk/    # 本仓库（版本管理源头）
├── SKILL.md / CLAUDE.md
└── scripts/leverage_risk.py
```

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

## Common Modifications

- **更新流通市值参考**：修改 `REFERENCE_MV`（或运行时 `--cyb-mv/--kcb-mv/--total-mv`）
- **调整风险阈值**：`RISK_THRESHOLD` 或 `--threshold`
- **新增板块**：在 `margin_by_board` 添加前缀规则，并补充 `REFERENCE_MV`
