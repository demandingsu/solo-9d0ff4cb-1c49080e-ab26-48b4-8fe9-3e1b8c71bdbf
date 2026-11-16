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


class ReviewNotFound(Exception):
    """待核记录不存在（HTTP 404）。"""


class StaleReviewError(Exception):
    """待核记录已不再处于待核状态（HTTP 409）：审批失败，原值与待核状态不变。

    `status` 是记录当前所处的状态（applied / kept / superseded）。
    """

    def __init__(self, review_id: str, status: str):
        self.review_id = review_id
        self.status = status
        super().__init__(f"待核记录 {review_id} 当前状态为 {status}，不能再做决定")


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


# ---------------------------------------------------------------- 可复核导入


def _swipe_values(row) -> tuple[str, str, str]:
    return (row["swipe_time_utc"], row["line"], row["temp_station_id"])


def _supersede_stale_pending(
    conn, txn_id: str, current: tuple[str, str, str]
) -> int:
    """失效所有"原值已不是库中现值"的待核记录。

    这些记录的原值基准已经被别处（审批/上传）改掉，
    再审批也无法证明是对同一原值的决定。
    """
    cur = conn.execute(
        "UPDATE swipe_reviews SET status = 'superseded', decided_at = ?, note = ? "
        "WHERE txn_id = ? AND status = 'pending' "
        "  AND NOT (base_swipe_time_utc = ? AND base_line = ? AND base_temp_station_id = ?)",
        (_now(), "库中原值已变化，待核失效", txn_id, *current),
    )
    return cur.rowcount


def import_swipes_reviewable(rows: list[SwipeRow], filename: str) -> dict:
    """可复核导入：文件内同号异内容仍整批拒收；与库冲突的行转入待核，
    其余新流水立即入库；同号同内容跳过。全部在一个写事务内完成，
    新流水、待核记录与月度统计一起提交。"""
    # 1) 文件内部：同号不同内容 -> 整批拒收（与原导入规则一致）
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
        batch_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO import_batches "
            "(batch_id, file_type, filename, rows_received, rows_applied, rows_skipped, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (batch_id, "swipes", filename, len(rows), 0, 0, None, _now()),
        )

        to_insert: list[SwipeRow] = []
        skipped = 0
        pending_new = 0
        pending_duplicate = 0
        superseded = 0
        review_ids: list[str] = []
        now = _now()

        for r in by_id.values():
            ex = conn.execute(
                "SELECT swipe_time_utc, line, temp_station_id "
                "FROM swipes WHERE txn_id = ?",
                (r.txn_id,),
            ).fetchone()

            # 2) 同号同内容：幂等跳过
            if ex is not None and _swipe_values(ex) == (
                r.swipe_time_utc,
                r.line,
                r.temp_station_id,
            ):
                skipped += 1
                # 内容已回到当前值：此前基于别的原值挂起的待核自然失效
                superseded += _supersede_stale_pending(conn, r.txn_id, _swipe_values(ex))
                continue

            # 3) 新流水：立即入库
            if ex is None:
                to_insert.append(r)
                continue

            # 4) 与库中冲突：留作待核（原值快照 + 文件新值）
            base = _swipe_values(ex)
            new = (r.swipe_time_utc, r.line, r.temp_station_id)

            # 4a) 同一原值下已挂起过同样的新内容 -> 不重复挂起
            dup = conn.execute(
                "SELECT review_id FROM swipe_reviews "
                "WHERE status = 'pending' AND txn_id = ? "
                "  AND base_swipe_time_utc = ? AND base_line = ? AND base_temp_station_id = ? "
                "  AND new_swipe_time_utc = ? AND new_line = ? AND new_temp_station_id = ?",
                (r.txn_id, *base, *new),
            ).fetchone()
            if dup is not None:
                pending_duplicate += 1
                superseded += _supersede_stale_pending(conn, r.txn_id, base)
                review_ids.append(dup["review_id"])
                continue

            # 4b) 其他原值基准的待核已失效，本次按当前库值重新挂起
            superseded += _supersede_stale_pending(conn, r.txn_id, base)

            review_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO swipe_reviews "
                "(review_id, txn_id, status, "
                " base_swipe_time_utc, base_line, base_temp_station_id, "
                " new_swipe_time_utc, new_line, new_temp_station_id, "
                " batch_id, filename, created_at) "
                "VALUES (?,?,'pending',?,?,?,?,?,?,?,?,?)",
                (review_id, r.txn_id, *base, *new, batch_id, filename, now),
            )
            review_ids.append(review_id)
            pending_new += 1

        conn.executemany(
            "INSERT INTO swipes (txn_id, swipe_time_utc, line, temp_station_id, batch_id) "
            "VALUES (?,?,?,?,?)",
            [
                (r.txn_id, r.swipe_time_utc, r.line, r.temp_station_id, batch_id)
                for r in to_insert
            ],
        )
        applied = len(to_insert)

        conn.execute(
            "UPDATE import_batches SET rows_applied = ?, rows_skipped = ?, note = ? "
            "WHERE batch_id = ?",
            (
                applied,
                skipped,
                f"待核新增 {pending_new} 条，重传已挂起 {pending_duplicate} 条，"
                f"旧待核失效 {superseded} 条",
                batch_id,
            ),
        )
        _recompute_stats(conn)
        return {
            "batch_id": batch_id,
            "file_type": "swipes",
            "filename": filename,
            "rows_received": len(rows),
            "applied": applied,
            "skipped_duplicates": skipped,
            "pending_new": pending_new,
            "pending_duplicate": pending_duplicate,
            "superseded": superseded,
            "review_ids": review_ids,
            "note": f"冲突待核 {pending_new} 条，未见冲突的新流水已入库 {applied} 条",
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


def _review_dict(row) -> dict:
    return dict(row)


_REVIEW_COLUMNS = (
    "review_id, txn_id, status, "
    "base_swipe_time_utc, base_line, base_temp_station_id, "
    "new_swipe_time_utc, new_line, new_temp_station_id, "
    "batch_id, filename, created_at, decided_at, note"
)


def list_reviews(
    status: str | None = "pending",
    txn_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """待核记录查询：默认只看 pending，可按流水号过滤。"""
    clauses: list[str] = []
    args: list = []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if txn_id:
        clauses.append("txn_id = ?")
        args.append(txn_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM swipe_reviews{where}", args
        ).fetchone()["c"]
        rows = conn.execute(
            f"SELECT {_REVIEW_COLUMNS} FROM swipe_reviews{where} "
            "ORDER BY created_at DESC, review_id LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
        return {"total": total, "items": [_review_dict(r) for r in rows]}
    finally:
        conn.close()


def get_review(review_id: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {_REVIEW_COLUMNS} FROM swipe_reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            raise ReviewNotFound(review_id)
        return _review_dict(row)
    finally:
        conn.close()


def decide_review(review_id: str, decision: str) -> dict:
    """按条决定：keep 保留原值，apply 采用新值。

    采用新值时，把流水改成文件里的新内容并按**当前映射**重算归站和月客流；
    决定、流水变更与统计在同一事务内提交；任何一步失败整体回滚，
    流水保持原值、待核记录保持原状态。

    审批前提是"库中现值仍等于待核记录里的原值"。
    别的审批先采用新值时会即时把该流水下其它待核置为 superseded，
    所以旧记录再审批直接返回 409；即便遇到原值被别处改变的情形，
    本次审批也整体回滚 —— 原值与待核状态都保持不变。
    要按新的现值复核，必须重新上传该文件。
    重复审批同一条记录同样返回 409。"""
    if decision not in ("keep", "apply"):
        raise ValueError(f"unknown decision: {decision}")

    result: dict | None = None
    now = _now()

    with write_txn() as conn:
        row = conn.execute(
            f"SELECT {_REVIEW_COLUMNS} FROM swipe_reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            raise ReviewNotFound(review_id)
        if row["status"] != "pending":
            raise StaleReviewError(review_id, row["status"])

        base = (
            row["base_swipe_time_utc"],
            row["base_line"],
            row["base_temp_station_id"],
        )
        new = (
            row["new_swipe_time_utc"],
            row["new_line"],
            row["new_temp_station_id"],
        )
        current = conn.execute(
            "SELECT swipe_time_utc, line, temp_station_id "
            "FROM swipes WHERE txn_id = ?",
            (row["txn_id"],),
        ).fetchone()

        # 审批必须建立在"库中现值 == 待核原值"之上。
        # 若别的审批/导入先改了该流水，本记录的原值基准已不成立：
        # 审批失败（抛出 -> 整体回滚），原值与待核状态都不变。
        # 正常路径下，先成功的 apply 已把同流水其它待核置为 superseded。
        if current is None or _swipe_values(current) != base:
            raise StaleReviewError(review_id, "superseded")
        if decision == "keep":
            conn.execute(
                "UPDATE swipe_reviews SET status = 'kept', decided_at = ?, note = ? "
                "WHERE review_id = ?",
                (now, "审批：保留原值", review_id),
            )
            result = {
                "review": _review_row_after(conn, review_id),
                "swipe": dict(current),
                "stats_recomputed": False,
            }
        else:
            conn.execute(
                "UPDATE swipes SET swipe_time_utc = ?, line = ?, temp_station_id = ?, "
                "batch_id = ? WHERE txn_id = ?",
                (*new, row["batch_id"], row["txn_id"]),
            )
            conn.execute(
                "UPDATE swipe_reviews SET status = 'applied', decided_at = ?, note = ? "
                "WHERE review_id = ?",
                (now, "审批：采用新值", review_id),
            )
            # 同一条流水下其它待核以旧原值为基准，一并失效
            conn.execute(
                "UPDATE swipe_reviews SET status = 'superseded', decided_at = ?, note = ? "
                "WHERE txn_id = ? AND status = 'pending' AND review_id <> ?",
                (now, "同一流水已被另一审批改为新值，旧待核失效", row["txn_id"], review_id),
            )
            _recompute_stats(conn)  # 按当前映射重新归站、重算月客流
            updated = conn.execute(
                "SELECT swipe_time_utc, line, temp_station_id, batch_id "
                "FROM swipes WHERE txn_id = ?",
                (row["txn_id"],),
            ).fetchone()
            result = {
                "review": _review_row_after(conn, review_id),
                "swipe": dict(updated),
                "stats_recomputed": True,
            }

    assert result is not None
    return result


def _review_row_after(conn, review_id: str) -> dict:
    return dict(
        conn.execute(
            f"SELECT {_REVIEW_COLUMNS} FROM swipe_reviews WHERE review_id = ?",
            (review_id,),
        ).fetchone()
    )


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
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM swipe_reviews WHERE status = 'pending'"
        ).fetchone()["c"]
        superseded = conn.execute(
            "SELECT COUNT(*) AS c FROM swipe_reviews WHERE status = 'superseded'"
        ).fetchone()["c"]
        return {
            "swipes": swipes,
            "mapping_intervals": mappings,
            "mapping_groups": groups,
            "import_batches": batches,
            "monthly_stat_rows": stats_rows,
            "attributed_boardings": months,
            "pending_reviews": pending,
            "superseded_reviews": superseded,
        }
    finally:
        conn.close()
