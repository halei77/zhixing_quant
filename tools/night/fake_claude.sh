#!/usr/bin/env bash
# 测试替身：模拟无头 claude 会话（stream-json 输出），供 night_driver 离线测试。
# 不打真实 API——机制本身要可测，否则「没人跑的 tools/ 脚本会烂掉」（bench_storage 同款教训）。
#
# 用法：NIGHT_CLAUDE_BIN=tools/night/fake_claude.sh FAKE_MODE=success|partial|hangup|fat|destructive|slow
#   success      正常收工：合格交接棒，退出 0
#   partial      部分完成：合格交接棒 + 开着的口子（进决策包）
#   hangup       会话中途崩：不写交接棒，退出 1（驱动应代写挂起桩）
#   fat          交接棒 200 行：触发容量违规
#   destructive  执行了 git reset --hard：触发司机硬复查违规
#   slow         挂死：验证墙钟超时杀进程
set -u
mode="${FAKE_MODE:-success}"
handoff="${NIGHT_HANDOFF_PATH:?fake_claude 需要 NIGHT_HANDOFF_PATH}"

emit() { printf '%s\n' "$1"; }
emit '{"type":"system","subtype":"init","session_id":"fake"}'

write_handoff() {  # $1=状态行（完成|挂起|部分完成）$2=结论行 $3=口子行
  mkdir -p "$(dirname "$handoff")"
  cat > "$handoff" << EOF
# 交接棒 · ${NIGHT_TASK_ID:-?} · $1

## 一、结论与产出物（≤10 行）

- $2

## 二、关键决策（逐条：选了什么 / 为什么 / 代价是什么）

- 选了最小改动 / 因为任务边界只到这里 / 代价是后续要补测试

## 三、触碰的文件（仅路径）

- src/zhixing_quant/example.py

## 四、证据指针（pytest 尾行、coverage 数字、backtest_id、commit sha、CI run id）

- pytest 1360 passed；coverage 94.9%；commit abc1234

## 五、开着的口子 / 待用户裁决（标注对应 Q1 哪个节点）

- $3

## 六、建议的下一任务（一句话 + 路线图 Step 依据）

- 接着做同 Step 的下一条（依据 01 路线图）
EOF
}

case "$mode" in
  slow)
    sleep 300
    exit 0
    ;;
  hangup)
    emit '{"type":"assistant","message":{"content":[{"type":"text","text":"中途崩了"}]}}'
    exit 1
    ;;
  destructive)
    emit '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"git reset --hard HEAD~1"}}]}}'
    write_handoff "完成" "假装做完了" "无"
    emit '{"type":"result","subtype":"success"}'
    exit 0
    ;;
  fat)
    write_handoff "完成" "超长交接棒" "无"
    for _ in $(seq 1 180); do echo "填充行，模拟把日志贴进交接棒的行为" >> "$handoff"; done
    exit 0
    ;;
  partial)
    write_handoff "部分完成" "主体已落，剩一件没做完" "剩的那件：要不要放宽 R011 容差（Q1 测试权限节点）"
    emit '{"type":"result","subtype":"success"}'
    exit 0
    ;;
  *)
    write_handoff "完成" "任务完成，产出见文件清单" "无"
    emit '{"type":"result","subtype":"success"}'
    exit 0
    ;;
esac
