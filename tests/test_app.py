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


# ---------------------------------------------------------------- 可复核导入

REVIEW_HEADER = b"txn_id,swipe_time_utc,line,temp_station_id\n"
MAPPING_HEADER = (
    b"line,temp_station_id,valid_from_utc,valid_to_utc,real_station\n"
)


def review_upload(client, name, body):
    return upload(client, "/api/imports/swipes/reviewable", name, body)


def reviews(client, **params):
    r = client.get("/api/reviews", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def decide(client, review_id, decision):
    return client.post(f"/api/reviews/{review_id}/decision", json={"decision": decision})


def test_reviewable_import_conflict_goes_pending_others_imported(client):
    """与库冲突的行待核，其余新流水立即入库，同内容跳过；统计同步提交。"""
    m = (
        MAPPING_HEADER
        + b"L1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Alpha\n"
        + b"L1,T2,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Beta\n"
    )
    assert upload(client, "/api/imports/mappings", "m.csv", m).status_code == 200
    first = (
        REVIEW_HEADER
        + b"A1,2026-01-05T08:00:00Z,L1,T1\n"
        + b"A2,2026-01-05T09:00:00Z,L1,T1\n"
    )
    assert upload(client, "/api/imports/swipes", "first.csv", first).status_code == 200
    assert stats_map(client) == {("2026-01", "Alpha"): 2}

    # A1 改内容（冲突待核），A2 同内容（跳过），A3 新流水（立即入库）
    second = (
        REVIEW_HEADER
        + b"A1,2026-01-05T08:30:00Z,L1,T2\n"
        + b"A2,2026-01-05T09:00:00Z,L1,T1\n"
        + b"A3,2026-01-06T10:00:00Z,L1,T2\n"
    )
    r = review_upload(client, "second.csv", second)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 1
    assert body["skipped_duplicates"] == 1
    assert body["pending_new"] == 1 and body["pending_duplicate"] == 0
    # 无冲突的新流水立刻计入；冲突流水仍按原值计数
    assert stats_map(client) == {("2026-01", "Alpha"): 2, ("2026-01", "Beta"): 1}

    page = reviews(client)
    assert page["total"] == 1
    item = page["items"][0]
    assert item["txn_id"] == "A1" and item["status"] == "pending"
    assert (item["base_line"], item["base_temp_station_id"]) == ("L1", "T1")
    assert item["base_swipe_time_utc"].startswith("2026-01-05T08:00:00")
    assert (item["new_line"], item["new_temp_station_id"]) == ("L1", "T2")
    assert item["new_swipe_time_utc"].startswith("2026-01-05T08:30:00")
    assert item["filename"] == "second.csv"
    assert client.get(f"/api/reviews/{item['review_id']}").status_code == 200
    assert client.get("/api/reviews/not-exist").status_code == 404


def test_reviewable_import_within_file_conflict_rejected(client):
    body = (
        REVIEW_HEADER
        + b"A1,2026-01-05T08:00:00Z,L1,T1\n"
        + b"A1,2026-01-05T09:00:00Z,L1,T1\n"
    )
    r = review_upload(client, "dup.csv", body)
    assert r.status_code == 409
    assert client.get("/api/overview").json()["swipes"] == 0
    assert reviews(client)["total"] == 0


def test_reupload_same_base_and_content_does_not_duplicate_pending(client):
    assert upload(
        client, "/api/imports/swipes", "a.csv",
        REVIEW_HEADER + b"A1,2026-01-05T08:00:00Z,L1,T1\n",
    ).status_code == 200
    conflict = REVIEW_HEADER + b"A1,2026-01-05T08:30:00Z,L1,T2\n"
    r1 = review_upload(client, "c1.csv", conflict)
    r2 = review_upload(client, "c2.csv", conflict)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["pending_new"] == 1
    assert r2.json()["pending_new"] == 0
    assert r2.json()["pending_duplicate"] == 1
    assert r2.json()["review_ids"] == r1.json()["review_ids"]
    page = reviews(client)
    assert page["total"] == 1
    assert client.get("/api/overview").json()["pending_reviews"] == 1


def test_apply_decision_recomputes_attribution(client):
    """采用新值时按当前映射重算归站与月客流。"""
    m = (
        MAPPING_HEADER
        + b"L1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Alpha\n"
        + b"L1,T2,2026-02-01T00:00:00Z,2026-03-01T00:00:00Z,Beta\n"
    )
    assert upload(client, "/api/imports/mappings", "m.csv", m).status_code == 200
    assert upload(
        client, "/api/imports/swipes", "a.csv",
        REVIEW_HEADER + b"A1,2026-01-05T08:00:00Z,L1,T1\n",
    ).status_code == 200
    assert stats_map(client) == {("2026-01", "Alpha"): 1}

    # 新值：换站且换月
    r = review_upload(
        client, "c.csv",
        REVIEW_HEADER + b"A1,2026-02-05T08:00:00Z,L1,T2\n",
    )
    rid = r.json()["review_ids"][0]
    assert stats_map(client) == {("2026-01", "Alpha"): 1}  # 待核未生效

    d = decide(client, rid, "apply")
    assert d.status_code == 200, d.text
    body = d.json()
    assert body["review"]["status"] == "applied"
    assert body["stats_recomputed"] is True
    assert body["swipe"]["temp_station_id"] == "T2"
    assert stats_map(client) == {("2026-02", "Beta"): 1}
    assert client.get("/api/transactions/unattributed").json()["total"] == 0


def test_keep_decision_preserves_original(client):
    m = (
        MAPPING_HEADER
        + b"L1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Alpha\n"
        + b"L1,T2,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Beta\n"
    )
    assert upload(client, "/api/imports/mappings", "m.csv", m).status_code == 200
    assert upload(
        client, "/api/imports/swipes", "a.csv",
        REVIEW_HEADER + b"A1,2026-01-05T08:00:00Z,L1,T1\n",
    ).status_code == 200
    r = review_upload(
        client, "c.csv", REVIEW_HEADER + b"A1,2026-01-05T08:30:00Z,L1,T2\n"
    )
    rid = r.json()["review_ids"][0]
    d = decide(client, rid, "keep")
    assert d.status_code == 200, d.text
    assert d.json()["review"]["status"] == "kept"
    assert d.json()["swipe"]["line"] == "L1"
    assert d.json()["swipe"]["temp_station_id"] == "T1"
    assert stats_map(client) == {("2026-01", "Alpha"): 1}
    # 已决定过的记录不能再次决定
    assert decide(client, rid, "apply").status_code == 409


def test_stale_decision_invalidated_by_other_approval(client):
    """别的审批先改了该流水：旧待核决定失效，重传后才能按现值复核。"""
    m = (
        MAPPING_HEADER
        + b"L1,T1,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Alpha\n"
        + b"L1,T2,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Beta\n"
        + b"L1,T3,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Gamma\n"
    )
    assert upload(client, "/api/imports/mappings", "m.csv", m).status_code == 200
    assert upload(
        client, "/api/imports/swipes", "a.csv",
        REVIEW_HEADER + b"A1,2026-01-05T08:00:00Z,L1,T1\n",
    ).status_code == 200

    # 两个文件对同一流水给出不同新值 -> 两条待核，原值相同
    r_b = review_upload(client, "b.csv", REVIEW_HEADER + b"A1,2026-01-05T09:00:00Z,L1,T2\n")
    r_c = review_upload(client, "c.csv", REVIEW_HEADER + b"A1,2026-01-05T10:00:00Z,L1,T3\n")
    rid_b = r_b.json()["review_ids"][0]
    rid_c = r_c.json()["review_ids"][0]
    assert reviews(client)["total"] == 2

    # 采用 B：C 的待核立即失效
    assert decide(client, rid_b, "apply").status_code == 200
    c_item = client.get(f"/api/reviews/{rid_c}").json()
    assert c_item["status"] == "superseded"
    # 用失效记录审批 -> 409，流水保持 B，统计不变
    before = stats_map(client)
    stale = decide(client, rid_c, "apply")
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["status"] == "superseded"
    assert stats_map(client) == before

    # 重新上传 C 的内容 -> 以 B 为原值重新挂起，可正常采用
    again = review_upload(client, "c-again.csv", REVIEW_HEADER + b"A1,2026-01-05T10:00:00Z,L1,T3\n")
    assert again.json()["pending_new"] == 1
    rid_c2 = again.json()["review_ids"][0]
    assert rid_c2 != rid_c
    item = client.get(f"/api/reviews/{rid_c2}").json()
    assert item["base_temp_station_id"] == "T2" and item["new_temp_station_id"] == "T3"
    assert decide(client, rid_c2, "apply").status_code == 200
    assert stats_map(client) == {("2026-01", "Gamma"): 1}


def test_approval_failure_rolls_back_swipe_and_review(client, monkeypatch):
    """审批中途失败：流水保持原值，待核记录保持 pending，统计不变。"""
    from app import service

    assert upload(
        client, "/api/imports/swipes", "a.csv",
        REVIEW_HEADER + b"A1,2026-01-05T08:00:00Z,L1,T1\n",
    ).status_code == 200
    r = review_upload(
        client, "c.csv", REVIEW_HEADER + b"A1,2026-01-05T09:00:00Z,L1,T2\n"
    )
    rid = r.json()["review_ids"][0]

    def boom(conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_recompute_stats", boom)
    with pytest.raises(RuntimeError):
        service.decide_review(rid, "apply")

    item = client.get(f"/api/reviews/{rid}").json()
    assert item["status"] == "pending"
    swipe_row = client.get("/api/reviews", params={"txn_id": "A1", "status": "all"}).json()
    assert swipe_row["items"][0]["new_temp_station_id"] == "T2"
    # 流水未被改
    import sqlite3, os
    conn = sqlite3.connect(os.environ["TRANSIT_DB_PATH"])
    try:
        row = conn.execute(
            "SELECT temp_station_id FROM swipes WHERE txn_id='A1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "T1"


def test_strict_import_still_rejects_conflicts(client):
    """原有导入接口行为不变：同号不同内容仍整批拒收。"""
    assert upload(
        client, "/api/imports/swipes", "a.csv",
        REVIEW_HEADER + b"A1,2026-01-05T08:00:00Z,L1,T1\n",
    ).status_code == 200
    r = upload(
        client, "/api/imports/swipes", "b.csv",
        REVIEW_HEADER
        + b"A1,2026-01-05T09:00:00Z,L1,T1\n"
        + b"A2,2026-01-06T09:00:00Z,L1,T1\n",
    )
    assert r.status_code == 409
    assert client.get("/api/overview").json()["swipes"] == 1
    assert reviews(client)["total"] == 0  # 严格导入不产生待核


def test_reupload_after_apply_does_not_create_new_pending(client):
    """采用新值后重传同一文件：内容已一致，跳过且不再挂起。"""
    assert upload(
        client, "/api/imports/swipes", "a.csv",
        REVIEW_HEADER + b"A1,2026-01-05T08:00:00Z,L1,T1\n",
    ).status_code == 200
    conflict = REVIEW_HEADER + b"A1,2026-01-05T08:30:00Z,L1,T2\n"
    r = review_upload(client, "c.csv", conflict)
    rid = r.json()["review_ids"][0]
    assert decide(client, rid, "apply").status_code == 200
    assert client.get("/api/overview").json()["pending_reviews"] == 0

    again = review_upload(client, "c-again.csv", conflict)
    assert again.status_code == 200
    assert again.json()["applied"] == 0
    assert again.json()["pending_new"] == 0
    assert again.json()["pending_duplicate"] == 0
    assert again.json()["skipped_duplicates"] == 1
    assert client.get("/api/overview").json()["pending_reviews"] == 0
    # 已采用记录再次审批 -> 409，数据不变
    assert decide(client, rid, "keep").status_code == 409


def test_decision_invalid_when_swipe_changed_without_superseding(client):
    """极端情形：待核原值已被别处改掉但记录仍 pending，审批整体回滚。"""
    from app import service

    assert upload(
        client, "/api/imports/swipes", "a.csv",
        REVIEW_HEADER + b"A1,2026-01-05T08:00:00Z,L1,T1\n",
    ).status_code == 200
    r = review_upload(
        client, "c.csv", REVIEW_HEADER + b"A1,2026-01-05T09:00:00Z,L1,T2\n"
    )
    rid = r.json()["review_ids"][0]

    # 绕过审批直接改库，模拟"别的途径改了原值"
    from app import db

    with db.write_txn() as conn:
        conn.execute(
            "UPDATE swipes SET temp_station_id = 'T9' WHERE txn_id = 'A1'"
        )
    with pytest.raises(service.StaleReviewError):
        service.decide_review(rid, "apply")
    # 审批失败：待核状态不变、流水保持 T9
    item = client.get(f"/api/reviews/{rid}").json()
    assert item["status"] == "pending"
    assert client.get(f"/api/reviews/{rid}").json()["new_temp_station_id"] == "T2"
