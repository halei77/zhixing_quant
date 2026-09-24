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
# report_rc 是远期 PE 的原料（ADR-0022 决定 2）——2026-09-25 又差点漏了它（同族第二次）：
# 新接一张参考表就该同步这行，否则生产远期PE 段一律「本节无数据」。
# fina_indicator 是 ROE/增速序列（fina_trend）的原料（06 §十-5，2026-09-25 接）——同族第三张，
# 漏了它生产上「建仓价分析」「长期投资」的 ROE/增速段一律「本节无数据」。
for t in daily_basic forecast stk_limit fina_audit stk_holdernumber index_daily report_rc fina_indicator; do
  [ -d "/home/lei/zhixing_data/data/$t" ] || continue
  rsync -az --delete "/home/lei/zhixing_data/data/$t" aliyun:/opt/zhixing_data/data/
done

# 1 分钟K（ADR-0022 决定 3 的 minute_1）**年分区截断**：只发「当前年−1」起的 year=*。
#
# 为什么截断（交付策略定案，见 reports/design-2026-09-24-site-data-expansion.md §三）：
# 服务器是 40G 盘、实测空闲 34G，而 minute_1 全市场外推 **12.4 GB/年**（132 万行/日 × 39 B/行，
# 按 minute_60 实测的 38.9 B/行口径）——发全历史 5 年约 63 GB，**第 3 个自然年就爆盘**。
# 两年窗峰值约 25 GB，跨年后 `--delete` 自动把最老年份清掉。
#
# **全历史留在家里**（ADR-0006 的分工：重计算在家、云只服务站点）——站点模板对分钟最深只要
# 30 天（1 分K 自定义封顶 30，site/api 那条），两年窗是冗余保险；真要查全历史回家里跑。
# 冷热分层（站点按需远端拉取）被否：破 ADR-0012 决定 2「生成之路上不联网」。
if [ -d /home/lei/zhixing_data/data/minute_1 ]; then
  KEEP_FROM=$(( $(date +%Y) - 1 ))
  _ex=()
  for _d in /home/lei/zhixing_data/data/minute_1/year=*; do
    [ -e "$_d" ] || continue
    _y=${_d##*year=}
    if [ "$_y" -lt "$KEEP_FROM" ]; then _ex+=(--exclude "year=$_y"); fi
  done
  # `${_ex[@]+...}`：空数组在 `set -u` 下展开会炸，这是 bash 的老坑
  rsync -az --delete ${_ex[@]+"${_ex[@]}"} \
    /home/lei/zhixing_data/data/minute_1 aliyun:/opt/zhixing_data/data/
fi

# golden 整目录发，不再点名文件——2026-09-25 之前点名 4 个，于是 #57 加的停牌两份快照没发，
# 「服务器缺它 zhixing-site 起不来」（#57 的 agent 当时手工补的）。**新接一份主数据快照就该
# 自动跟上**，点名清单是第三种同族漏（前两种：参考表漏发、report_rc 漏列）。
rsync -az --delete /home/lei/zhixing_data/golden/ aliyun:/opt/zhixing_data/golden/

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
# 除了 HTTP 200 还要看**磁盘余量**——minute_1 两年窗落地后 `/` 使用约 30G/40G、余约 10G
# （交付策略的代价②）。只 curl 200 只证明进程活着，盘满了它照样 200 然后明天写不进去。
ssh aliyun "curl -s -o /dev/null -w '远端健康：HTTP %{http_code}\n' http://127.0.0.1:8000/ \
  && df -h / | awk 'NR==2 {printf \"远端磁盘：%s 已用 / %s 可用（%s）\\n\", \$3, \$4, \$5}' \
  && du -sh /opt/zhixing_data/data/minute_1 2>/dev/null || echo '远端 minute_1：未发'"

echo "发布完成 $(date '+%F %T')"
