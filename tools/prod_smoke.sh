#!/usr/bin/env bash
# 生产端到端回归（只读）：部署后跑一遍，全绿才算「生产实测过」。
#
# 为什么是 curl 集不是 playwright：服务器**无 node/无 playwright**（2026-09-24 实测 which 全空），
# 所以服务器侧只能 curl。playwright 留在本地 e2e（tools/e2e/，打 127.0.0.1）；这条脚本打的是
# **生产进程**，验的是「发上去的东西在跑」，与本地 e2e 是两件事，不要互相替代。
#
# 只读：刻意不碰 `POST /api/recent`（那会写盘）。
# 用法：tools/prod_smoke.sh   （退出码 0 = 全绿；非 0 = 有红，看输出定位）
set -uo pipefail
cd "$(dirname "$0")/.."

BASE="${ZX_PROD_BASE:-https://kline.43.108.88.176.sslip.io}"
# 服务器上的进程地址（健康检查打它，绕开 Caddy 反代）：127.0.0.1:8000
LOCAL="http://127.0.0.1:8000"
fail=0

chk() {  # chk <描述> <条件真值 1/0>
  if [ "$2" = "1" ]; then printf '  PASS  %s\n' "$1"; else printf '  FAIL  %s\n' "$1"; fail=1; fi
}

echo "== 1) 进程与页面 =="
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/")
chk "公网 $BASE/ 返回 200（实测 $code）" "$([ "$code" = 200 ] && echo 1 || echo 0)"
active=$(ssh aliyun 'systemctl is-active zhixing-site' 2>/dev/null)
chk "zhixing-site 服务 active（实测 $active）" "$([ "$active" = active ] && echo 1 || echo 0)"

echo "== 2) 模板面（ready 至少 3 条）=="
ready=$(curl -s "$BASE/api/templates" | python3 -c "
import json,sys
t=json.load(sys.stdin)['templates']
print(sum(1 for x in t if x['status']=='ready'))")
chk "ready 模板数 ≥3（实测 $ready）" "$([ "$ready" -ge 3 ] 2>/dev/null && echo 1 || echo 0)"

echo "== 3) 搜索 =="
# 用 --data-urlencode 让 curl 自己编——**别手写百分号编码**。2026-09-25 实测踩过：
# 手写的 %E6%AF%9B%E5%88%A9 是「毛利」不是「茅 台」（茅=E8%8C%85、台=E5%8F%B0），
# 于是搜「贵州毛利」返回 0，被误判成搜索坏了。
hits=$(curl -s -G "$BASE/api/search" --data-urlencode 'q=贵州茅台' | python3 -c "
import json,sys; print(len(json.load(sys.stdin)['hits']))")
chk "搜「贵州茅台」有候选（实测 $hits 条）" "$([ "$hits" -ge 1 ] 2>/dev/null && echo 1 || echo 0)"

echo "== 4) K线（个股 + 指数）=="
for u in "code=600519&days=120" "code=000001.SH&kind=index&days=30"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/kline?$u")
  chk "/api/kline?$u → 200（实测 $code）" "$([ "$code" = 200 ] && echo 1 || echo 0)"
done

echo "== 5) 主模板提示词（含基本面头 + 参考表段）=="
body=$(curl -s -X POST "$BASE/api/prompt" -H 'Content-Type: application/json' \
  -d '{"code":"600519","template":"短期投资"}')
python3 - "$body" <<'PY' > /tmp/prod_smoke_5.txt
import json,sys
d=json.loads(sys.argv[1])
t=d.get('text','') or d.get('detail','')
heads=[x[:40] for x in t.splitlines() if x.startswith('###')]
print('tokens', d.get('tokens'))
print('warn', d.get('warn'))
print('has_head', any(h.startswith('### 最新基本面') for h in heads))
print('has_valuation', any('估值' in h for h in heads))
print('has_limit', any('涨跌停' in h for h in heads))
print('has_index', any('大盘' in h for h in heads))
print('no_data', t.count('本节无数据'))
print('heads', ' | '.join(heads))
PY
. /tmp/prod_smoke_5.txt 2>/dev/null || cat /tmp/prod_smoke_5.txt
tok=$(awk '/^tokens/{print $2}' /tmp/prod_smoke_5.txt)
chk "短期投资出 token 数（实测 $tok）" "$([ -n "$tok" ] && [ "$tok" != "None" ] && echo 1 || echo 0)"
chk "含最新基本面头" "$(awk '/^has_head/{print $2}' /tmp/prod_smoke_5.txt | grep -q True && echo 1 || echo 0)"
chk "含估值段" "$(awk '/^has_valuation/{print $2}' /tmp/prod_smoke_5.txt | grep -q True && echo 1 || echo 0)"
chk "含涨跌停段" "$(awk '/^has_limit/{print $2}' /tmp/prod_smoke_5.txt | grep -q True && echo 1 || echo 0)"
chk "含大盘段（短期投资要有）" "$(awk '/^has_index/{print $2}' /tmp/prod_smoke_5.txt | grep -q True && echo 1 || echo 0)"
# 远期 PE（用户点名项，C 刀）：与日K 同窗——120 天日K 必须配 120 天远期 PE
fwd=$(curl -s -X POST "$BASE/api/prompt" -H 'Content-Type: application/json' \
  -d '{"code":"600519","template":"短期投资"}' | python3 -c "
import json,sys,re
t=json.load(sys.stdin).get('text','')
m=re.search(r'### 远期PE[^\n]*', t)
print(m.group(0) if m else 'NONE')")
echo "  远期PE段题：$fwd"
chk "远期PE段与日K 同窗（120 个交易日）" "$(echo "$fwd" | grep -q '120 个交易日' && echo 1 || echo 0)"

echo "== 6) 估值段非空（2026-09-24 事故回归点：publish 漏发参考表）=="
empty=$(curl -s -X POST "$BASE/api/prompt" -H 'Content-Type: application/json' \
  -d '{"code":"600519","template":"长期投资"}' | grep -c '本节无数据' || true)
chk "长期投资估值段非空（本节无数据 ×$empty，期望估值段不出）" "$([ "$empty" -le 1 ] 2>/dev/null && echo 1 || echo 0)"

echo "== 7) 缺数据的诚实交代（300308 的分钟段）=="
heads=$(curl -s -X POST "$BASE/api/prompt" -H 'Content-Type: application/json' \
  -d '{"code":"300308","template":"短期投资"}' | python3 -c "
import json,sys,re; t=json.load(sys.stdin).get('text',''); print('|'.join(re.findall(r'### .+', t)))")
echo "  段题：$heads"
chk "300308 出日K段" "$(echo "$heads" | grep -q '日K' && echo 1 || echo 0)"

echo "== 8) pending 模板拒生成（400）=="
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/prompt" \
  -H 'Content-Type: application/json' -d '{"code":"600519","template":"建仓价分析"}')
chk "建仓价分析（pending）→ 400（实测 $code）" "$([ "$code" = 400 ] && echo 1 || echo 0)"

echo "== 9) 自定义组合（含 1 分K 封顶）=="
code=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -X POST "$BASE/api/prompt" \
  -H 'Content-Type: application/json' \
  -d '{"code":"600519","template":"自定义","custom":[{"dataset":"minute_60","days":30}]}')
chk "自定义 minute_60×30 → 200（实测 $code）" "$(echo "$code" | grep -q '^200' && echo 1 || echo 0)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/prompt" \
  -H 'Content-Type: application/json' \
  -d '{"code":"600519","template":"自定义","custom":[{"dataset":"minute_1","days":31}]}')
chk "自定义 minute_1×31 → 400（封顶 30，实测 $code）" "$([ "$code" = 400 ] && echo 1 || echo 0)"

echo "== 10) 数据新鲜度 + 磁盘（minute_1 年窗落地后必查）=="
ssh aliyun "df -h / | awk 'NR==2 {printf \"  远端磁盘：%s 已用 / %s 可用（%s）\\n\", \$3, \$4, \$5} \
  && du -sh /opt/zhixing_data/data/minute_1 2>/dev/null || echo \"  远端 minute_1：未发\"" 2>/dev/null
avail=$(ssh aliyun "df -BG / | awk 'NR==2 {gsub(/G/,\"\",\$4); print \$4}'" 2>/dev/null)
chk "磁盘可用 ≥5G（实测 ${avail}G）" "$([ -n "$avail" ] && [ "$avail" -ge 5 ] 2>/dev/null && echo 1 || echo 0)"

echo
if [ "$fail" = 0 ]; then echo "== 生产回归全绿 =="; else echo "== 生产回归有红（见上方 FAIL）=="; fi
exit $fail
