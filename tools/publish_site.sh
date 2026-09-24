#!/usr/bin/env bash
# 数据发布（ADR-0006：WSL2 计算 → 验证 → 发布 → 阿里云）。
# 规模（2026-09-24 实测）：行情四 dataset 518.6 MB / 33,238 文件 + 六张参考表 74 MB + golden
# 1.5 MB ≈ **636 MB**——注释早期写的「≈4MB」在全市场 60 分首采后就失真了，别照它估带宽。
# rsync 增量「秒级」那句仍然成立，但**只对**比对（dry-run 3.0 s / 33k 文件）成立；要传的
# **字节数**才是变量（实测上传带宽 6.75 MB/s）。回填类操作跑完要手动跑一次本脚本——远端
# 只有发过去的东西，回填后不发，生产估值段就是「本节无数据」（2026-09-24 踩过两次）。
# 用法：tools/publish_site.sh （幂等；跑完打印远端健康检查一行）
set -euo pipefail
cd "$(dirname "$0")/.."

rsync -az --delete \
  /home/lei/zhixing_data/data/daily \
  /home/lei/zhixing_data/data/minute_5 \
  /home/lei/zhixing_data/data/minute_30 \
  /home/lei/zhixing_data/data/minute_60 \
  aliyun:/opt/zhixing_data/data/

# 参考表（ADR-0015）：估值/预告/涨跌停等。**必须一起发**——2026-09-24 实测服务器上
# daily_basic 是空的（本地 80M），于是「长期投资」的估值段在生产一律出「本节无数据」。
# 只发行情四 dataset 的那版漏了这一段，提示词站点二期的估值组件等于没上线。
# 目录不存在的跳过（fina_audit/stk_holdernumber 尚未采集是常态），不因此让整次发布失败。
for t in daily_basic forecast stk_limit fina_audit stk_holdernumber index_daily; do
  [ -d "/home/lei/zhixing_data/data/$t" ] || continue
  rsync -az --delete "/home/lei/zhixing_data/data/$t" aliyun:/opt/zhixing_data/data/
done

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
