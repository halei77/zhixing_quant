"""黄金样本抓取：把 akshare 的真实响应落成可重放的 CSV（03 §二 L2；Step 2a）。

为什么样本先落在数据根（`$ZX_DATA_ROOT/golden/`）而不是直接进仓：真实快照进仓等于把
"外部服务器那天给了什么"变成仓库历史的一部分——它是数据不是代码，而且一次落错就会永久
重放一个错答案。所以这里只负责**抓下来给人看**；纳入 `tests/golden/` 是 05 Q1-③ 的确认
节点，要用户批准（样本进仓之后就是断言的一部分，改它等于改测试口径，05 Q3）。

抓取边界可注入：本模块自己不发请求，抓取函数由 `build_fetchers` 给。测试用假 fetcher 往
tmp 目录里落，于是 CSV 读写与清单格式是被测代码；真联网的那几个函数只在手动跑
`uv run tools/capture_golden.py` 时才被调用，CI 无网络也过得了这一层。

失败时不假装成功：某个源挂了就把它的名字与原因写进清单，其余照落，退出码 1。静默少一个
文件比多一个错误更糟——那正是"样本还在、内容已过期"的由来。
"""

from __future__ import annotations

import csv
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from zhixing_quant.config import golden_dir

#: 一行源数据。适配器收到的就是这个形状，样本落盘再读回来也必须是它。
Row = Mapping[str, Any]
Fetcher = Callable[[], Sequence[Row]]
Record = dict[str, object]

#: 清单列。captured_at 记到秒：同一天重抓两次也要分得出先后。
MANIFEST_COLUMNS = (
    "key",
    "file",
    "rows",
    "columns",
    "captured_at",
    "status",
    "detail",
)


#: 清单文件名。每次抓取覆盖它：清单说的是"这批样本为什么长这样"，过期清单比没有更误导人。
MANIFEST_NAME = "manifest.csv"


def capture(
    fetchers: Mapping[str, Fetcher], out_dir: Path, *, captured_at: datetime
) -> list[Record]:
    """逐个抓取并落盘，返回清单行（也留一份在内存里，供调用方判成败）。

    快照文件名 = `<key>.csv`，key 里带源与参数（见 `build_fetchers`）：换窗口就是换 key。
    覆盖同名文件等于把一份已批准的样本悄悄换掉，所以 key 不允许复用。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[Record] = []
    for key in sorted(fetchers):
        name = f"{key}.csv"
        record: Record = {
            "key": key,
            "file": name,
            "rows": "",
            "columns": "",
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "status": "failed",
            "detail": "",
        }
        try:
            data = fetchers[key]()
        except Exception as exc:  # 任何一个源都可能挂：挂的进清单，其余照落
            record["detail"] = f"{type(exc).__name__}: {exc}"
        else:
            record["rows"] = write_csv(data, out_dir / name)
            record["columns"] = "|".join(column_names(data))
            # 空样本重放不出任何断言。当成成功会让"源今天什么都没给"看起来像"一切正常"。
            record["status"] = "ok" if data else "empty"
            if not data:
                record["detail"] = "源返回 0 行"
        records.append(record)
    write_manifest(records, out_dir / MANIFEST_NAME)
    return records


def column_names(rows: Sequence[Row]) -> tuple[str, ...]:
    """列名按首次出现的顺序取并集。

    不排序是故意的：列序是源的一部分，排过序的样本重放回来的行和适配器当初收到的行不是
    一回事，"漂移了哪一列"也就看不出来了。
    """
    seen: dict[str, None] = {}
    for row in rows:
        for name in row:
            seen.setdefault(name, None)
    return tuple(seen)


def write_csv(rows: Sequence[Row], path: Path) -> int:
    """行 → CSV，返回写出的行数。

    只用文本协议，不落 pickle/parquet：样本要能 diff、要在评审时被人指着说"这列变了"。
    二进制格式做得到同样的事，做不到"先看数据再看代码"。
    """
    columns = column_names(rows)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(row.get(name)) for name in columns])
    return len(rows)


def read_csv(path: Path) -> list[dict[str, object]]:
    """样本读回来。空串还原成 None，其余保持字符串——归一是适配器的活，不在这里做。"""
    with path.open(encoding="utf-8", newline="") as fh:
        return [
            {name: (None if value == "" else value) for name, value in row.items()}
            for row in csv.DictReader(fh)
        ]


def _cell(value: object) -> str:
    """一格的文本形。

    NaN/inf 落成 `nan`/`inf` 而不是空串，为的是保真：空串读回来是 None，门禁会报"字段
    没给"，而源当时给的是一个不成立的数——这两种诊断在日报上不是同一件事（`sources/rows`
    的整条取向就在这里）。`nan` 同时是 pandas `read_csv` 默认的缺失值之一，两边都不误读。
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def write_manifest(records: Iterable[Record], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(MANIFEST_COLUMNS))
        writer.writeheader()
        writer.writerows(records)


# --- 真实抓取（只有手动运行时才走到）------------------------------------------------


def _fetch_akshare(function: str, **kwargs: object) -> Sequence[Row]:
    """调 akshare 并把 DataFrame 转成行列表。

    `import akshare` 待在这里而不是模块顶部：样本重放测试、CI 与任何不联网的场合都不该为
    它付导入代价。akshare 没有类型标注，所以这里立刻落到 `list[dict]`——本模块往下没有
    pandas 类型要标注。
    """
    from importlib import import_module

    ak: Any = import_module("akshare")
    frame: Any = getattr(ak, function)(**kwargs)
    return [dict(row) for row in frame.to_dict(orient="records")]


def build_fetchers(window: tuple[str, str], symbols: tuple[str, ...]) -> dict[str, Fetcher]:
    """这次抓取要落哪些样本：窗口与代码写在这一处，不散落在调用处。

    不复权帧与后复权帧都要落：`adj_factor` 是两者相除得来的，只落一帧的样本重放不出因子，
    R005 就没有判据来源。

    key 的约定是 `<函数名>__<参数>`，双下划线分隔：key 直接可做文件名，参数一眼可见。
    """
    start, end = window
    fetchers: dict[str, Fetcher] = {
        "tool_trade_date_hist_sina": lambda: _fetch_akshare("tool_trade_date_hist_sina"),
        "stock_info_sh_name_code__主板A股": lambda: _fetch_akshare(
            "stock_info_sh_name_code", symbol="主板A股"
        ),
        "stock_info_sz_name_code__A股列表": lambda: _fetch_akshare(
            "stock_info_sz_name_code", symbol="A股列表"
        ),
    }
    for symbol in symbols:
        fetchers[f"stock_zh_a_daily__{symbol}__{start}_{end}__raw"] = _daily_fetcher(
            symbol, start, end, adjust=""
        )
        fetchers[f"stock_zh_a_daily__{symbol}__{start}_{end}__hfq"] = _daily_fetcher(
            symbol, start, end, adjust="hfq"
        )
    return fetchers


def _daily_fetcher(symbol: str, start: str, end: str, *, adjust: str) -> Fetcher:
    """把参数绑成闭包的实参，而不是留给循环变量：后者会让所有 key 都抓最后一只票。"""
    return lambda: _fetch_akshare(
        "stock_zh_a_daily", symbol=symbol, start_date=start, end_date=end, adjust=adjust
    )


def main(argv: list[str] | None = None) -> int:
    """`capture_golden.py [start end] [symbols,csv]`，默认抓 2024 年 1 月的两只票。

    默认窗口是写死在这里的：黄金样本报的是"源在某个已知窗口里给了什么形状"，窗口跟着日期
    每天变会让样本每次重抓都是新数据，漂移与更新就分不开了。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    window = (args[0], args[1]) if len(args) >= 2 else ("20240102", "20240131")
    symbols = tuple(args[2].split(",")) if len(args) >= 3 else ("sh600519", "sz300750")
    out_dir = golden_dir()
    records = capture(build_fetchers(window, symbols), out_dir, captured_at=datetime.now())
    for record in records:
        print(f"{record['status']:5} {record['key']} rows={record['rows']}")
    bad = [r for r in records if r["status"] != "ok"]
    if bad:
        print(
            f"{len(bad)} 个样本没抓到，原因见 {out_dir / MANIFEST_NAME}",
            file=sys.stderr,
        )
        return 1
    print(f"样本已落到 {out_dir}；纳入 tests/golden/ 需用户批准（05 Q1-③）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
