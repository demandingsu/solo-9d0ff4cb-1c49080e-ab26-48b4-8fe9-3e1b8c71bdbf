import os
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRANSIT_DB_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def upload(client, url, filename, content: bytes):
    return client.post(url, files={"file": (filename, content, "text/csv")})


def upload_sample(client, kind, name):
    return upload(client, f"/api/imports/{kind}", name, (SAMPLES / name).read_bytes())


def stats_map(client, month=None):
    params = {"month": month} if month else {}
    r = client.get("/api/stats/monthly", params=params)
    assert r.status_code == 200
    return {(i["month"], i["real_station"]): i["boardings"] for i in r.json()}


# ---------------------------------------------------------------- 故事主线


def test_full_story_with_samples(client):
    """样例文件完整走一遍：迟到改站重算 -> 幂等重传 -> 冲突拒收 -> 重叠拒收。"""
    # 1. 先导流水：还没有任何映射，全部未归属
    r = upload_sample(client, "swipes", "swipes_1.csv")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 10 and body["skipped_duplicates"] == 0
    assert stats_map(client) == {}
    una = client.get("/api/transactions/unattributed").json()
    assert una["total"] == 10

    # 2. 导入映射：旧流水立即被重新归属（统计与导入同一事务提交）
    r = upload_sample(client, "mappings", "mappings_1_initial.csv")
    assert r.status_code == 200, r.text
    assert stats_map(client) == {
        ("2026-01", "Central Station"): 2,            # S001, S002
        ("2026-01", "Riverside"): 1,                  # S003
        ("2026-02", "Central Station North Gate"): 1, # S004
        ("2026-02", "Museum"): 1,                     # S005
        ("2026-02", "Riverside"): 2,                  # S009(+08:00 归一), S010
    }
    una = client.get("/api/transactions/unattributed").json()
    assert una["total"] == 3  # S006 超出时段, S007 超出时段, S008 无映射

    # 3. 迟到改站：整组替换 (L1,T100)，3 月窗口延长，S007 被重新归属
    r = upload_sample(client, "mappings", "mappings_2_late_correction.csv")
    assert r.status_code == 200, r.text
    s = stats_map(client)
    assert s[("2026-03", "Central Station North Gate")] == 1  # S007
    assert client.get("/api/transactions/unattributed").json()["total"] == 2

    # 4. 更多流水：S001 同号同内容 -> 跳过不重复计数
    r = upload_sample(client, "swipes", "swipes_2_more.csv")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 2 and r.json()["skipped_duplicates"] == 1
    s = stats_map(client)
    assert s[("2026-01", "Central Station")] == 2  # 没有变成 3
    assert s[("2026-03", "Central Station North Gate")] == 2  # S007 + S011
    assert s[("2026-03", "Riverside")] == 1                   # S012

    # 5. 同号不同内容 -> 409 整批拒收，S013 也不入库，统计不变
    before = stats_map(client)
    r = upload_sample(client, "swipes", "swipes_3_conflict.csv")
    assert r.status_code == 409, r.text
    assert "S001" in str(r.json()["detail"]["conflicts"])
    assert stats_map(client) == before
    assert client.get("/api/overview").json()["swipes"] == 12  # S013 未入库

    # 6. 组内时段重叠 -> 409 整批拒收，(L1,T200) 旧映射不变
    r = upload_sample(client, "mappings", "mappings_3_overlap.csv")
    assert r.status_code == 409, r.text
    assert "重叠" in str(r.json()["detail"]["conflicts"])
    t200 = client.get("/api/mappings", params={"line": "L1", "temp_station_id": "T200"}).json()
    assert len(t200) == 1 and t200[0]["real_station"] == "Museum"
    assert stats_map(client) == before


# ---------------------------------------------------------------- 规则细节


def test_idempotent_reimport_same_file(client):
    csv1 = b"txn_id,swipe_time_utc,line,temp_station_id\nA1,2026-01-05T08:00:00Z,L1,T1\n"
    m = b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\nL1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,X\n"
    assert upload(client, "/api/imports/swipes", "a.csv", csv1).status_code == 200
    assert upload(client, "/api/imports/mappings", "m.csv", m).status_code == 200
    first = stats_map(client)
    r = upload(client, "/api/imports/swipes", "a.csv", csv1)
    assert r.status_code == 200
    assert r.json()["applied"] == 0 and r.json()["skipped_duplicates"] == 1
    assert stats_map(client) == first  # 不重复计数


def test_same_id_different_content_rejected_atomically(client):
    csv1 = b"txn_id,swipe_time_utc,line,temp_station_id\nA1,2026-01-05T08:00:00Z,L1,T1\n"
    csv2 = b"txn_id,swipe_time_utc,line,temp_station_id\nA1,2026-01-05T09:00:00Z,L1,T1\nA2,2026-01-06T09:00:00Z,L1,T1\n"
    assert upload(client, "/api/imports/swipes", "a.csv", csv1).status_code == 200
    r = upload(client, "/api/imports/swipes", "b.csv", csv2)
    assert r.status_code == 409
    assert client.get("/api/overview").json()["swipes"] == 1  # A2 也未入库


def test_within_file_same_id_different_content_rejected(client):
    csv1 = b"txn_id,swipe_time_utc,line,temp_station_id\nA1,2026-01-05T08:00:00Z,L1,T1\nA1,2026-01-05T09:00:00Z,L1,T1\n"
    r = upload(client, "/api/imports/swipes", "a.csv", csv1)
    assert r.status_code == 409
    assert client.get("/api/overview").json()["swipes"] == 0


def test_group_replacement_drops_old_intervals(client):
    m1 = (b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
          b"L1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,X\n"
          b"L1,T1,2026-02-01T00:00:00Z,2026-03-01T00:00:00Z,Y\n"
          b"L2,T1,2026-01-01T00:00:00Z,2026-03-01T00:00:00Z,Z\n")
    m2 = (b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
          b"L1,T1,2026-01-15T00:00:00Z,2026-02-15T00:00:00Z,X2\n")
    assert upload(client, "/api/imports/mappings", "m1.csv", m1).status_code == 200
    assert upload(client, "/api/imports/mappings", "m2.csv", m2).status_code == 200
    rows = client.get("/api/mappings").json()
    l1 = [r for r in rows if r["line"] == "L1"]
    l2 = [r for r in rows if r["line"] == "L2"]
    assert len(l1) == 1 and l1[0]["real_station"] == "X2"  # 整组替换
    assert len(l2) == 1 and l2[0]["real_station"] == "Z"   # 未涉及的组不受影响


def test_temp_station_reused_by_different_lines(client):
    m = (b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
         b"L1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Alpha\n"
         b"L2,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Beta\n")
    s = (b"txn_id,swipe_time_utc,line,temp_station_id\n"
         b"A1,2026-01-10T08:00:00Z,L1,T1\n"
         b"A2,2026-01-10T08:05:00Z,L2,T1\n")
    assert upload(client, "/api/imports/mappings", "m.csv", m).status_code == 200
    assert upload(client, "/api/imports/swipes", "s.csv", s).status_code == 200
    assert stats_map(client) == {("2026-01", "Alpha"): 1, ("2026-01", "Beta"): 1}


def test_unattributed_reasons(client):
    m = (b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
         b"L1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,X\n")
    s = (b"txn_id,swipe_time_utc,line,temp_station_id\n"
         b"A1,2026-03-01T08:00:00Z,L1,T1\n"   # 有映射但不在时段内
         b"A2,2026-01-10T08:00:00Z,L9,T9\n")  # 完全没有映射
    assert upload(client, "/api/imports/mappings", "m.csv", m).status_code == 200
    assert upload(client, "/api/imports/swipes", "s.csv", s).status_code == 200
    una = client.get("/api/transactions/unattributed").json()
    assert una["total"] == 2
    reasons = {i["txn_id"]: i["reason"] for i in una["items"]}
    assert "时段" in reasons["A1"] and "没有任何映射" in reasons["A2"]


def test_malformed_csv_422(client):
    # 缺列
    r = upload(client, "/api/imports/swipes", "bad.csv", b"txn_id,line\nA1,L1\n")
    assert r.status_code == 422
    # 时间无法解析
    r = upload(client, "/api/imports/swipes", "bad.csv",
               b"txn_id,swipe_time_utc,line,temp_station_id\nA1,not-a-time,L1,T1\n")
    assert r.status_code == 422
    # 映射起止倒置
    r = upload(client, "/api/imports/mappings", "bad.csv",
               b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
               b"L1,T1,2026-02-01T00:00:00Z,2026-01-01T00:00:00Z,X\n")
    assert r.status_code == 422
    # 空文件
    r = upload(client, "/api/imports/swipes", "bad.csv", b"")
    assert r.status_code == 422
    assert client.get("/api/overview").json()["import_batches"] == 0


def test_month_filter_and_boundary(client):
    """时段为左闭右开 [from, to)：边界时刻归属正确。"""
    m = (b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
         b"L1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Jan\n"
         b"L1,T1,2026-02-01T00:00:00Z,2026-03-01T00:00:00Z,Feb\n")
    s = (b"txn_id,swipe_time_utc,line,temp_station_id\n"
         b"A1,2026-01-31T23:59:59Z,L1,T1\n"
         b"A2,2026-02-01T00:00:00Z,L1,T1\n")
    assert upload(client, "/api/imports/mappings", "m.csv", m).status_code == 200
    assert upload(client, "/api/imports/swipes", "s.csv", s).status_code == 200
    assert stats_map(client, "2026-01") == {("2026-01", "Jan"): 1}
    assert stats_map(client, "2026-02") == {("2026-02", "Feb"): 1}


# ---------------------------------------------------------------- 并发一致性


def test_concurrent_imports_and_consistent_reads(tmp_path, monkeypatch):
    """两类文件并发导入：任何已提交快照里 统计 + 未归属 == 流水总数。"""
    monkeypatch.setenv("TRANSIT_DB_PATH", str(tmp_path / "concurrent.db"))
    from app import db, service
    from app.csvio import MappingRow, SwipeRow

    db.init_db()
    errors = []

    def import_swipes(tag):
        try:
            for k in range(5):
                rows = [
                    SwipeRow(f"{tag}-{k}-{i}", "2026-01-10T08:00:00.000000Z", "L1", "T1")
                    for i in range(20)
                ]
                service.import_swipes(rows, f"{tag}-{k}.csv")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def import_mappings():
        try:
            for k in range(5):
                rows = [
                    MappingRow(
                        "L1", "T1",
                        "2026-01-01T00:00:00.000000Z",
                        f"2026-0{k + 2}-01T00:00:00.000000Z",
                        f"StationV{k}",
                    )
                ]
                service.import_mappings(rows, f"m-{k}.csv")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def read_invariant():
        try:
            sql = """
                SELECT (SELECT COUNT(*) FROM swipes) AS total,
                       (SELECT COALESCE(SUM(boardings), 0) FROM monthly_stats) AS attributed,
                       (SELECT COUNT(*) FROM swipes s WHERE NOT EXISTS (
                            SELECT 1 FROM mappings m
                            WHERE m.line = s.line AND m.temp_station_id = s.temp_station_id
                              AND s.swipe_time_utc >= m.valid_from_utc
                              AND s.swipe_time_utc <  m.valid_to_utc)) AS unattributed
            """
            for _ in range(30):
                conn = db.connect()
                try:
                    row = conn.execute(sql).fetchone()
                finally:
                    conn.close()
                assert row["attributed"] + row["unattributed"] == row["total"]
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = (
        [threading.Thread(target=import_swipes, args=(t,)) for t in "AB"]
        + [threading.Thread(target=import_mappings)]
        + [threading.Thread(target=read_invariant) for _ in range(2)]
    )
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    ov = service.overview()
    assert ov["swipes"] == 200
    # 最终状态：所有流水都已归属（最终映射覆盖 2026-01）
    final = service.monthly_stats()
    assert sum(i["boardings"] for i in final) == 200
    assert service.unattributed(1000, 0)["total"] == 0
