# 夜间任务简报（全新 session，上下文必须小）

先执行 README §5 起手式 6 步。第 0 步：{prev_handoff_line}
读文档按指针分段读，**禁止整文件 Read 大文件**（历史教训：单次 500KiB 的读盘会让此后每轮请求都重付一次）；搜索广度活交给子代理，只拿结论回来。

本次唯一任务：{task_id} {title}
- 关联 Step：{step}｜验收文档：{refs}
- 起点 commit：{git_sha}

边界（不可外推）：
- 只做本任务。`zx-task` 状态推进最多到**数据验证**；未登记的任务先 `zx-task new`（09 §八-1）。
- 禁止：`conclude` / `resume` / `abandon`、黄金样本更新、文档采纳（可起草，采纳留决策包）、策略合入 main、`tools/publish_site.sh`、`git push --force` / `reset --hard` / `clean` / `branch -D`。
- 门禁全绿才 commit（Conventional Commits，一提交一事，信息写「为什么」）：
  `uv run ruff check`、`uv run mypy`、`uv run pytest`、`tools/check_coverage.py`
- 节奏：红灯迭代 ≤3 轮仍不绿 → 不硬凑，写「挂起」交接棒收工；墙钟到 60 分钟未收敛 → 同样收口，剩下的写进「开着的口子」。

收工前必做：按 `tools/night/handoff.tmpl.md` 写交接棒到 **{handoff_path}**（≤150 行）。
低风险自主决策记「关键决策」：选了什么 / 为什么 / 代价是什么（05 规则 7）；Q1 四节点事项只进「待用户裁决」。
