# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**leverage-risk** — 市场杠杆风险监测技能。计算 A 股主板、创业板、科创板融资余额占各自流通市值的比例，>4% 触发风险警示。

## Commands

```bash
# 运行分析（使用内置参考流通市值）
python3 scripts/leverage_risk.py

# 传入最新流通市值（交易所官方统计数据，单位：亿元）
python3 scripts/leverage_risk.py --cyb-mv 144934.15 --kcb-mv 107381.71

# 指定融资余额日期 + JSON 输出
python3 scripts/leverage_risk.py --date 20260717 --json

# 自定义风险阈值
python3 scripts/leverage_risk.py --threshold 5.0
```

## Architecture

### 数据流

```
AKShare (唯一外部依赖)
  ├── stock_margin_detail_szse(date) → 深市融资融券明细
  └── stock_margin_detail_sse(date)  → 沪市融资融券明细
        ↓
  按股票代码前缀分类汇总
  ├── 创业板: 300/301 开头 (深市)
  ├── 科创板: 688 开头 (沪市)
  ├── 深市主板: 000/002/003 开头 (深市)
  └── 沪市主板: 60 开头 (沪市)
        ↓
  融资余额(元) / 流通市值(亿元×1e8) × 100 = 占比(%)
        ↓
  占比 > threshold → 风险警示
```

### 关键设计决策

- **流通市值无实时接口**：各板块总流通市值没有公开 API，通过 CLI 参数传入（`--cyb-mv` `--kcb-mv` 等），未传时使用脚本内置参考值
- **板块分类**：按股票代码前缀区分，而非交易所市场字段。沪市主板仅取 60 开头（排除 688 科创板），深市主板排除 300/301
- **数据单位**：AKShare 融资余额为**元**，流通市值参数为**亿元**，计算时需 ×1e8 统一单位

### 文件结构

```
~/.claude/skills/leverage-risk/
├── SKILL.md                      # 技能触发文档（告诉 Claude 何时使用此技能）
├── CLAUDE.md                     # 本文件（开发指导）
└── scripts/
    └── leverage_risk.py          # 唯一脚本，全部逻辑集中于此
```

## Dependencies

```
akshare >= 1.0    # 唯一的外部数据源依赖
pandas >= 1.0     # 随 akshare 引入
```

仅依赖 Python 标准库 + akshare + pandas。**不使用** requests、re、任何直接 HTTP 请求。

## Reference Data

内置参考数据位于 `REFERENCE_DATA` 字典（流通市值，单位：亿元）：

| 板块 | 流通市值(亿) | 日期 |
|------|------------|------|
| 创业板 | 144,934.15 | 2026-07-17 |
| 科创板 | 107,381.71 | 2026-07-17 |
| 沪深两市 | 822,000.00 | 2026-07-17 |

主板流通市值未提供（显示 N/A），需用户传入或在脚本中补充。

## Validation

以 2026-07-17 官方数据为基准：

| 板块 | 融资余额(亿) | 官方占比 | 脚本结果 |
|------|------------|---------|---------|
| 创业板 | 6,188.66 | 4.3% | 4.26% |
| 科创板 | 3,648.79 | 3.4% | 3.39% |

微小差异（<0.05%）源于 AKShare 融资余额与官方统计存在约 0.18% 的统计时点差异。

## Common Modifications

- **更新参考流通市值**：修改 `REFERENCE_DATA["circulating_mv"]` 字典
- **调整风险阈值**：修改 `RISK_THRESHOLD` 常量（默认 4.0）
- **新增板块**：在 `BOARD_CONFIG` 风格的字典中添加前缀规则
