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

# 前端（ADR-0018 决定 7）：服务器无 node，故**本机构建、发构建产物**，不在服务器构建。
# `frontend/dist` 在 .gitignore 里，仓库镜像带不过去，所以单发这一步；node_modules 不发
# （它是缓存不是运行时）。发的是 /opt/zhixing_quant/frontend/dist——zx-site 的 served_dir()
# 读的正是它（ADR-0018 决定 2）。StaticFiles 逐请求读盘，不需要重启服务。
#
# 远端父目录必须显式建：`frontend/` 里除 dist 外都是入库源码，靠"重部署 rsync 仓库"才出现；
# 全新服务器 / 只跑数据发布 cron（未先重部署）时它不存在，rsync 的 mkdir 会以 code 11 失败
# （2026-09-23 17:40 cron 现场：rsync: mkdir "/opt/zhixing_quant/frontend/dist" failed: No such file）。
# `--delete` 只清 dist 内部，不会碰同级的源码，所以 mkdir -p 这一步是幂等且安全的。
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build
ssh aliyun "mkdir -p /opt/zhixing_quant/frontend/dist"
rsync -az --delete frontend/dist/ aliyun:/opt/zhixing_quant/frontend/dist/

# 健康检查放最后：它报的是本次发布跑完之后的线上状态，不是数据 rsync 之后、前端之前的状态。
ssh aliyun "curl -s -o /dev/null -w '远端健康：HTTP %{http_code}\n' http://127.0.0.1:8000/"

echo "发布完成 $(date '+%F %T')"
