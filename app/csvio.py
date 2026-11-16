"""CSV 解析与时间规范化。

所有时间在入库前统一规范为固定宽度的 UTC 文本
（YYYY-MM-DDTHH:MM:SS.ffffffZ），字典序即时间序，
因此 SQLite 里可以直接用字符串比较做时段匹配和月份截取。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone

SWIPE_COLUMNS = ["txn_id", "swipe_time_utc", "line", "temp_station_id"]
MAPPING_COLUMNS = [
    "line",
    "temp_station_id",
    "valid_from_utc",
    "valid_to_utc",
    "real_station",
]


class CsvError(Exception):
    """CSV 结构/内容非法（HTTP 422）：整批拒收。"""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(self.errors[0] if self.errors else "CSV 解析失败")


@dataclass(frozen=True)
class SwipeRow:
    txn_id: str
    swipe_time_utc: str
    line: str
    temp_station_id: str


@dataclass(frozen=True)
class MappingRow:
    line: str
    temp_station_id: str
    valid_from_utc: str
    valid_to_utc: str
    real_station: str


def parse_utc(value: str) -> datetime:
    """解析 ISO 8601 时间；无时区信息时按 UTC 处理，最终统一归一到 UTC。"""
    v = value.strip()
    if not v:
        raise ValueError("empty timestamp")
    if v[-1] in ("Z", "z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_utc(dt: datetime) -> str:
    """统一成固定宽度 UTC 文本，保证字典序 == 时间序。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _read_grid(raw: bytes, required: list[str]) -> list[tuple[int, dict[str, str]]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError([f"文件不是有效的 UTF-8 编码: {exc}"])
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        raise CsvError(["空文件：第一行必须是表头"])
    header = [h.strip() for h in rows[0]]
    missing = [c for c in required if c not in header]
    if missing:
        raise CsvError(
            [f"缺少必需列: {', '.join(missing)}；必需列为: {', '.join(required)}"]
        )
    idx = {name: header.index(name) for name in required}
    grid: list[tuple[int, dict[str, str]]] = []
    errors: list[str] = []
    for lineno, r in enumerate(rows[1:], start=2):
        if len(r) < len(header):
            errors.append(f"第 {lineno} 行: 列数不足（{len(r)} < {len(header)}）")
            continue
        grid.append((lineno, {name: r[i].strip() for name, i in idx.items()}))
    if errors:
        raise CsvError(errors)
    if not grid:
        raise CsvError(["文件没有数据行"])
    return grid


def parse_swipes(raw: bytes) -> list[SwipeRow]:
    grid = _read_grid(raw, SWIPE_COLUMNS)
    parsed: list[SwipeRow] = []
    errors: list[str] = []
    for lineno, rec in grid:
        if not rec["txn_id"] or not rec["line"] or not rec["temp_station_id"]:
            errors.append(
                f"第 {lineno} 行: txn_id / line / temp_station_id 不能为空"
            )
            continue
        try:
            t = fmt_utc(parse_utc(rec["swipe_time_utc"]))
        except ValueError:
            errors.append(
                f"第 {lineno} 行: swipe_time_utc 无法解析: {rec['swipe_time_utc']!r}"
            )
            continue
        parsed.append(
            SwipeRow(
                txn_id=rec["txn_id"],
                swipe_time_utc=t,
                line=rec["line"],
                temp_station_id=rec["temp_station_id"],
            )
        )
    if errors:
        raise CsvError(errors)
    return parsed


def parse_mappings(raw: bytes) -> list[MappingRow]:
    grid = _read_grid(raw, MAPPING_COLUMNS)
    parsed: list[MappingRow] = []
    errors: list[str] = []
    for lineno, rec in grid:
        if not rec["line"] or not rec["temp_station_id"] or not rec["real_station"]:
            errors.append(
                f"第 {lineno} 行: line / temp_station_id / real_station 不能为空"
            )
            continue
        try:
            frm = fmt_utc(parse_utc(rec["valid_from_utc"]))
            to = fmt_utc(parse_utc(rec["valid_to_utc"]))
        except ValueError:
            errors.append(f"第 {lineno} 行: 生效起止时间无法解析")
            continue
        if not frm < to:
            errors.append(f"第 {lineno} 行: 生效起 {frm} 必须早于生效止 {to}")
            continue
        parsed.append(
            MappingRow(
                line=rec["line"],
                temp_station_id=rec["temp_station_id"],
                valid_from_utc=frm,
                valid_to_utc=to,
                real_station=rec["real_station"],
            )
        )
    if errors:
        raise CsvError(errors)
    return parsed
