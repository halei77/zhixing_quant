#!/usr/bin/env bash
# 数据发布（ADR-0006：WSL2 计算 → 验证 → 发布 → 阿里云）。站点数据 ≈ 4MB，rsync 增量秒级。
# 用法：tools/publish_site.sh （幂等；跑完打印远端健康检查一行）
set -euo pipefail
cd "$(dirname "$0")/.."

rsync -az --delete \
  /home/lei/zhixing_data/data/daily \
  /home/lei/zhixing_data/data/minute_5 \
  /home/lei/zhixing_data/data/minute_30 \
  /home/lei/zhixing_data/data/minute_60 \
  aliyun:/opt/zhixing_data/data/

rsync -az --delete \
  /home/lei/zhixing_data/golden/manifest.csv \
  "/home/lei/zhixing_data/golden/stock_info_sh_name_code__主板A股.csv" \
  "/home/lei/zhixing_data/golden/stock_info_sz_name_code__A股列表.csv" \
  /home/lei/zhixing_data/golden/tool_trade_date_hist_sina.csv \
  aliyun:/opt/zhixing_data/golden/

ssh aliyun "curl -s -o /dev/null -w '远端健康：HTTP %{http_code}\n' http://127.0.0.1:8000/"
echo "发布完成 $(date '+%F %T')"
