"""导入与查询的核心业务逻辑。

规则实现：
- 流水：按 txn_id 幂等 —— 同号同内容跳过不重复计数，同号不同内容整批拒收；
- 映射：按 (线路, 临时站号) 整组替换；组内时段重叠则整批拒收；
- 统计：每次成功导入后在同一事务内全量重算月度真实站点上车量，
  因此迟到的映射会重新归属旧流水；
- 一致性：映射/流水/统计在同一事务提交，查询只读已提交快照。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .csvio import MappingRow, SwipeRow
from .db import connect, write_txn


class ConflictError(Exception):
    """业务冲突（HTTP 409）：整批拒收，数据库保持不变。"""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(self.errors[0] if self.errors else "conflict")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _recompute_stats(conn) -> None:
    """全量重算月度真实站点上车量（与导入在同一事务内）。

    匹配条件：同线路 + 同临时站号，且刷卡时刻落在映射生效时段
    [valid_from_utc, valid_to_utc) 内。组内时段已保证不重叠，
    因此一条流水最多命中一个真实站点，不会重复计数。
    """
    conn.execute("DELETE FROM monthly_stats")
    conn.execute(
        """
        INSERT INTO monthly_stats (month, real_station, boardings)
        SELECT substr(s.swipe_time_utc, 1, 7) AS month,
               m.real_station,
               COUNT(*) AS boardings
        FROM swipes s
        JOIN mappings m
          ON m.line = s.line
         AND m.temp_station_id = s.temp_station_id
         AND s.swipe_time_utc >= m.valid_from_utc
         AND s.swipe_time_utc <  m.valid_to_utc
        GROUP BY month, m.real_station
        """
    )


# ---------------------------------------------------------------- 导入


def import_swipes(rows: list[SwipeRow], filename: str) -> dict:
    # 1) 文件内部：同号不同内容 -> 整批拒收；同号同内容视为重复行，去重后跳过
    by_id: dict[str, SwipeRow] = {}
    intra: set[str] = set()
    for r in rows:
        prev = by_id.get(r.txn_id)
        if prev is not None and prev != r:
            intra.add(f"流水号 {r.txn_id}: 同一文件内出现不同内容")
        else:
            by_id[r.txn_id] = r
    if intra:
        raise ConflictError(sorted(intra))

    with write_txn() as conn:
        to_insert: list[SwipeRow] = []
        skipped = 0
        conflicts: list[str] = []
        for r in by_id.values():
            ex = conn.execute(
                "SELECT swipe_time_utc, line, temp_station_id "
                "FROM swipes WHERE txn_id = ?",
                (r.txn_id,),
            ).fetchone()
            if ex is None:
                to_insert.append(r)
            elif (ex["swipe_time_utc"], ex["line"], ex["temp_station_id"]) == (
                r.swipe_time_utc,
                r.line,
                r.temp_station_id,
            ):
                skipped += 1  # 同号同内容：幂等跳过，不重复计数
            else:
                conflicts.append(
                    f"流水号 {r.txn_id}: 已存在不同内容 "
                    f"(库中 {ex['swipe_time_utc']} / {ex['line']} / {ex['temp_station_id']}，"
                    f"本次 {r.swipe_time_utc} / {r.line} / {r.temp_station_id})"
                )
        if conflicts:
            # 抛出后事务回滚：整批拒收，已有数据不变
            raise ConflictError(conflicts)

        batch_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO import_batches "
            "(batch_id, file_type, filename, rows_received, rows_applied, rows_skipped, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (batch_id, "swipes", filename, len(rows), len(to_insert), skipped, None, _now()),
        )
        conn.executemany(
            "INSERT INTO swipes (txn_id, swipe_time_utc, line, temp_station_id, batch_id) "
            "VALUES (?,?,?,?,?)",
            [
                (r.txn_id, r.swipe_time_utc, r.line, r.temp_station_id, batch_id)
                for r in to_insert
            ],
        )
        _recompute_stats(conn)
        return {
            "batch_id": batch_id,
            "file_type": "swipes",
            "filename": filename,
            "rows_received": len(rows),
            "applied": len(to_insert),
            "skipped_duplicates": skipped,
            "note": None,
        }


def import_mappings(rows: list[MappingRow], filename: str) -> dict:
    # 按 (线路, 临时站号) 分组 —— 替换与重叠校验都以组为单位
    groups: dict[tuple[str, str], list[MappingRow]] = {}
    for r in rows:
        groups.setdefault((r.line, r.temp_station_id), []).append(r)

    # 组内时段重叠 -> 整批拒收（纯校验，先失败先返回，不写库）
    overlaps: list[str] = []
    for (line, ts), ivs in groups.items():
        ordered = sorted(ivs, key=lambda i: (i.valid_from_utc, i.valid_to_utc))
        for a, b in zip(ordered, ordered[1:]):
            if b.valid_from_utc < a.valid_to_utc:
                overlaps.append(
                    f"线路 {line} / 临时站号 {ts}: "
                    f"时段 [{a.valid_from_utc}, {a.valid_to_utc}) 与 "
                    f"[{b.valid_from_utc}, {b.valid_to_utc}) 重叠"
                )
    if overlaps:
        raise ConflictError(overlaps)

    with write_txn() as conn:
        batch_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO import_batches "
            "(batch_id, file_type, filename, rows_received, rows_applied, rows_skipped, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                batch_id,
                "mappings",
                filename,
                len(rows),
                len(rows),
                0,
                f"整组替换 {len(groups)} 个 (线路, 临时站号) 组",
                _now(),
            ),
        )
        for (line, ts), ivs in groups.items():
            # 整组替换：文件里出现的组整体覆盖旧记录；未出现的组不受影响
            conn.execute(
                "DELETE FROM mappings WHERE line = ? AND temp_station_id = ?",
                (line, ts),
            )
            conn.executemany(
                "INSERT INTO mappings "
                "(line, temp_station_id, valid_from_utc, valid_to_utc, real_station, batch_id) "
                "VALUES (?,?,?,?,?,?)",
                [
                    (line, ts, i.valid_from_utc, i.valid_to_utc, i.real_station, batch_id)
                    for i in ivs
                ],
            )
        _recompute_stats(conn)
        return {
            "batch_id": batch_id,
            "file_type": "mappings",
            "filename": filename,
            "rows_received": len(rows),
            "applied": len(rows),
            "skipped_duplicates": 0,
            "note": f"整组替换 {len(groups)} 个 (线路, 临时站号) 组",
        }


# ---------------------------------------------------------------- 查询


def monthly_stats(month: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT month, real_station, boardings FROM monthly_stats"
        args: tuple = ()
        if month:
            sql += " WHERE month = ?"
            args = (month,)
        sql += " ORDER BY month, real_station"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def unattributed(limit: int, offset: int) -> dict:
    """未归属流水：刷卡时刻没有命中任何映射时段。"""
    not_covered = """
        NOT EXISTS (
            SELECT 1 FROM mappings m
            WHERE m.line = s.line AND m.temp_station_id = s.temp_station_id
              AND s.swipe_time_utc >= m.valid_from_utc
              AND s.swipe_time_utc <  m.valid_to_utc)
    """
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM swipes s WHERE {not_covered}"
        ).fetchone()["c"]
        items = conn.execute(
            f"""
            SELECT s.txn_id, s.swipe_time_utc, s.line, s.temp_station_id,
                   CASE WHEN k.line IS NULL THEN '该线路+临时站号没有任何映射'
                        ELSE '刷卡时刻不在任何映射生效时段内' END AS reason
            FROM swipes s
            LEFT JOIN (SELECT DISTINCT line, temp_station_id FROM mappings) k
                   ON k.line = s.line AND k.temp_station_id = s.temp_station_id
            WHERE {not_covered}
            ORDER BY s.swipe_time_utc, s.txn_id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return {"total": total, "items": [dict(r) for r in items]}
    finally:
        conn.close()


def list_mappings(line: str | None = None, temp_station_id: str | None = None) -> list[dict]:
    conn = connect()
    try:
        sql = (
            "SELECT line, temp_station_id, valid_from_utc, valid_to_utc, "
            "real_station, batch_id FROM mappings"
        )
        clauses, args = [], []
        if line:
            clauses.append("line = ?")
            args.append(line)
        if temp_station_id:
            clauses.append("temp_station_id = ?")
            args.append(temp_station_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY line, temp_station_id, valid_from_utc"
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def list_batches() -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT batch_id, file_type, filename, rows_received, rows_applied, "
            "rows_skipped, note, created_at FROM import_batches "
            "ORDER BY created_at DESC, batch_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def overview() -> dict:
    conn = connect()
    try:
        swipes = conn.execute("SELECT COUNT(*) AS c FROM swipes").fetchone()["c"]
        mappings = conn.execute("SELECT COUNT(*) AS c FROM mappings").fetchone()["c"]
        groups = conn.execute(
            "SELECT COUNT(*) AS c FROM (SELECT DISTINCT line, temp_station_id FROM mappings)"
        ).fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) AS c FROM import_batches").fetchone()["c"]
        stats_rows = conn.execute("SELECT COUNT(*) AS c FROM monthly_stats").fetchone()["c"]
        months = conn.execute(
            "SELECT COALESCE(SUM(boardings), 0) AS s FROM monthly_stats"
        ).fetchone()["s"]
        return {
            "swipes": swipes,
            "mapping_intervals": mappings,
            "mapping_groups": groups,
            "import_batches": batches,
            "monthly_stat_rows": stats_rows,
            "attributed_boardings": months,
        }
    finally:
        conn.close()
