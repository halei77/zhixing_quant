#!/usr/bin/env bash
# 每日盘后流水线：采集（zx-daily 全市场 + zx-minute 60 分全市场 + zx-minute 1 分试采池）→ 发布。
# 装成工作日 17:00 cron（见 docs/10-运维手册.md §二）。
#
# 为什么串成一条而不是两条 cron：采集时长随源波动（全市场数千只票，慢的时候几十分钟），
# 而发布必须发生在采集之后——两条 cron 之间的固定间隔迟早被某天的慢源吃掉，串行没有这个假设。
#
# 采集失败（退出码非 0）**不阻断**发布：干净区里已有的数据照发，站点不会因为源挂了就停更。
# 各退出码分别落在日志末行（rc_collect / rc_minute / rc_minute1 / rc_publish），便于事后对账。
#
# zx-minute --periods 60 是 ADR-0021 决定 3 的落点：sina 无窗口参数、每次固定回最近 1970 根，
# **漏抓的日子滑出 tail 就永久没了**——所以 60 分钟是每日必采，不是"有空再采"。范围 = 全市场
# 60 分单周期（同 ADR 决定 1/2；5/30 分暂不采）。失败与日线同理，不阻断发布。
#
# zx-minute --periods 1 是 1 分钟K 的**试采池**增量（验收同款 5 只票，源 = ngw、串行 1.5s 起步
# ⇒ 5 只 × 2 请求 × 1.5s ≈ 15s/日，成本几乎为零）：让站点 minute_1 每天都是新的、R011 天天
# 对账。**全市场 1 分不在这里**：5558 只 × 2 请求 × 1.5s ≈ 4.6 h/日（innercode 缓存后 ≈2.3 h）
# + 12.4 GB/年 磁盘（docs/10 §二 实测外推），属决策包 #12 的用户裁决项——只建议不执行。
set -u
cd "$(dirname "$0")/.."
LOG="${ZX_DATA_ROOT:-/home/lei/zhixing_data}/reports/daily_pipeline.log"
{
  echo "===== pipeline start $(date '+%F %T') ====="
  .venv/bin/zx-daily
  rc_collect=$?
  echo "----- zx-daily rc=$rc_collect -----"
  .venv/bin/zx-minute --periods 60 --limit 6000 --attempts 3 --breaker 80
  rc_minute=$?
  echo "----- zx-minute rc=$rc_minute -----"
  .venv/bin/zx-minute --periods 1 --symbols 600519,300308,000001,000002,600000 --attempts 3
  rc_minute1=$?
  echo "----- zx-minute 1min rc=$rc_minute1 -----"
  tools/publish_site.sh
  rc_publish=$?
  echo "===== pipeline done $(date '+%F %T') rc_collect=$rc_collect rc_minute=$rc_minute rc_minute1=$rc_minute1 rc_publish=$rc_publish ====="
} >> "$LOG" 2>&1
