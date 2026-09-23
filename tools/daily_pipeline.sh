#!/usr/bin/env bash
# 每日盘后流水线：采集（zx-daily，全市场）→ 发布（publish_site.sh）。
# 装成工作日 17:00 cron（见 docs/10-运维手册.md §二）。
#
# 为什么串成一条而不是两条 cron：采集时长随源波动（全市场数千只票，慢的时候几十分钟），
# 而发布必须发生在采集之后——两条 cron 之间的固定间隔迟早被某天的慢源吃掉，串行没有这个假设。
#
# 采集失败（退出码非 0）**不阻断**发布：干净区里已有的数据照发，站点不会因为源挂了就停更。
# 两条退出码分别落在日志末行（rc_collect / rc_publish），便于事后对账。
set -u
cd "$(dirname "$0")/.."
LOG="${ZX_DATA_ROOT:-/home/lei/zhixing_data}/reports/daily_pipeline.log"
{
  echo "===== pipeline start $(date '+%F %T') ====="
  .venv/bin/zx-daily
  rc_collect=$?
  echo "----- zx-daily rc=$rc_collect -----"
  tools/publish_site.sh
  rc_publish=$?
  echo "===== pipeline done $(date '+%F %T') rc_collect=$rc_collect rc_publish=$rc_publish ====="
} >> "$LOG" 2>&1
