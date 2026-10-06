"""合规复核服务端到端测试（基于种子主数据 P1/P2 + 2025 年限值）。"""
from __future__ import annotations

import json
import threading

import pytest

SAMPLED_AT = "2025-06-01T08:00:00+08:00"
MANDATORY = ["ph", "residual_chlorine", "turbidity"]


def _register(client, partition="P1", sampled_at=SAMPLED_AT):
    r = client.post("/samples", json={"partition_code": partition, "sampled_at": sampled_at})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _put(client, sid, indicator, value, expected_version, request_no=None, check=200):
    body = {"indicator_code": indicator, "value": value, "expected_version": expected_version}
    if request_no is not None:
        body["request_no"] = request_no
    r = client.put(f"/samples/{sid}/readings/{indicator}", json=body)
    assert r.status_code == check, r.text
    return r


def _readings_ok(client, sid, values, start_version=0):
    """顺序录入 readings: {indicator: value}，返回录完后的版本号。"""
    version = start_version
    for i, (indicator, value) in enumerate(values.items()):
        r = _put(client, sid, indicator, value, version)
        body = r.json()
        assert body["reading_version"] == version + 1
        assert body["corrected"] is False
        version += 1
    return version


# ---------------------------------------------------------------- 基础

def test_health_and_seed(client):
    assert client.get("/health").json() == {"status": "ok"}
    codes = {x["code"] for x in client.get("/admin/indicators").json()["indicators"]}
    assert {"turbidity", "ph", "residual_chlorine"} <= codes
    detail = client.get("/admin/partitions/P1").json()
    assert set(detail["mandatory_indicators"]) == set(MANDATORY)
    assert len(detail["limits"]) == 3


def test_register_and_get_sample(client):
    sid = _register(client)
    r = client.get(f"/samples/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["reading_version"] == 0
    assert body["sampled_at"] == "2025-06-01T00:00:00Z"  # +08:00 归一到 UTC
    assert body["readings"] == {}


# ---------------------------------------------------------------- 限值区间

def test_limit_overlap_rejected(client):
    r = client.post(
        "/admin/partitions/P1/limits",
        json={
            "indicator_code": "turbidity",
            "max_value": 2.0,
            "valid_from": "2025-06-01T00:00:00Z",
            "valid_to": "2025-07-01T00:00:00Z",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "limit_interval_overlap"


def test_limit_adjacent_intervals_allowed(client):
    # 与 P2 余氯现有区间端点相接 [..,2026-01-01) 与 [2026-01-01,..) 不重叠
    r = client.post(
        "/admin/partitions/P2/limits",
        json={
            "indicator_code": "residual_chlorine",
            "min_value": 0.03,
            "max_value": 4.0,
            "valid_from": "2026-01-01T00:00:00Z",
            "valid_to": "2027-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 201, r.text


def test_invalid_limit_interval_rejected(client):
    r = client.post(
        "/admin/partitions/P1/limits",
        json={
            "indicator_code": "ph",
            "min_value": 8.5,
            "max_value": 6.5,
            "valid_from": "2027-01-01T00:00:00Z",
            "valid_to": "2027-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 422


def test_naive_datetime_rejected(client):
    r = client.post("/samples", json={"partition_code": "P1", "sampled_at": "2025-06-01T08:00:00"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 读数

def test_initial_entry_and_version(client):
    sid = _register(client)
    version = _readings_ok(
        client,
        sid,
        {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8},
    )
    assert version == 3
    assert client.get(f"/samples/{sid}").json()["reading_version"] == 3


def test_stale_version_conflict(client):
    sid = _register(client)
    _put(client, sid, "ph", 7.1, 0)
    r = _put(client, sid, "turbidity", 0.3, 0, check=409)
    assert r.json()["error"]["code"] == "version_conflict"
    assert r.json()["error"]["details"]["current_version"] == 1


def test_negative_reading_rejected(client):
    sid = _register(client)
    r = _put(client, sid, "ph", -0.1, 0, check=422)
    assert r.json()["error"]["code"] == "validation_error"


def test_non_mandatory_reading_rejected(client):
    # 新建一个仅含 ph 必测项的分区，读数时拒绝其他指标
    client.post("/admin/partitions", json={"code": "PX", "name": "测试分区"})
    client.post("/admin/partitions/PX/mandatory-indicators", json={"indicator_code": "ph"})
    client.post(
        "/admin/partitions/PX/limits",
        json={
            "indicator_code": "ph",
            "min_value": 6.5,
            "max_value": 8.5,
            "valid_from": "2025-01-01T00:00:00Z",
            "valid_to": "2026-01-01T00:00:00Z",
        },
    )
    sid = _register(client, partition="PX")
    r = _put(client, sid, "turbidity", 0.5, 0, check=422)
    assert r.json()["error"]["code"] == "not_mandatory_indicator"


def test_correction_requires_request_no(client):
    sid = _register(client)
    _put(client, sid, "ph", 7.1, 0)
    r = _put(client, sid, "ph", 7.3, 1, check=422)
    assert r.json()["error"]["code"] == "correction_requires_request_no"


def test_correction_idempotent_replay_and_conflict(client):
    sid = _register(client)
    _put(client, sid, "ph", 7.1, 0)
    # 更正
    r1 = _put(client, sid, "ph", 7.3, 1, request_no="REQ-1")
    assert r1.json()["corrected"] is True
    assert r1.json()["reading_version"] == 2
    # 同号重放 -> 原结果；版本不得再次自增
    r2 = _put(client, sid, "ph", 7.3, 1, request_no="REQ-1")
    assert r2.json() == r1.json()
    assert client.get(f"/samples/{sid}").json()["reading_version"] == 2
    # 同号异内容（期望值不同）-> 拒绝
    r3 = _put(client, sid, "ph", 7.4, 1, request_no="REQ-1", check=422)
    assert r3.json()["error"]["code"] == "idempotency_key_conflict"
    # 同号在另一个样本上使用也是“异内容”（fingerprint 含 sample_id）
    other = _register(client)
    r4 = _put(client, other, "ph", 7.3, 0, request_no="REQ-1", check=422)
    assert r4.json()["error"]["code"] == "idempotency_key_conflict"


# ---------------------------------------------------------------- 发布门禁

def test_publish_rejected_when_readings_incomplete(client):
    sid = _register(client)
    _readings_ok(client, sid, {"ph": 7.1, "turbidity": 0.2})
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": 2})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "readings_incomplete"
    assert "residual_chlorine" in r.json()["error"]["details"]["missing_indicators"]


def test_publish_rejected_without_effective_limit(client):
    # 2024-12-31 采样：种子限值 2025-01-01 起生效，无生效限值
    sid = _register(client, sampled_at="2024-12-31T16:00:00Z")
    _readings_ok(client, sid, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": 3})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_effective_limit"


def test_half_open_interval_boundary(client):
    # 采样时刻恰为限值右端点 2026-01-01T00:00:00Z：左闭右开，旧限值已失效
    # P2 余氯有 2026 年接续限值（见 test_limit_adjacent_intervals_allowed），
    # 但 P1 无 -> P1 发布报无生效限值
    sid = _register(client, partition="P1", sampled_at="2026-01-01T00:00:00Z")
    _readings_ok(client, sid, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": 3})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_effective_limit"
    detail = r.json()["error"]["details"]
    assert set(detail["indicators_without_limit"]) == set(MANDATORY)


def test_publish_qualified_and_snapshot(client):
    sid = _register(client)
    version = _readings_ok(client, sid, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": version})
    assert r.status_code == 201, r.text
    snap = r.json()
    assert snap["conclusion"] == "qualified"
    assert snap["reading_version"] == 3
    assert snap["sample_id"] == sid
    assert len(snap["items"]) == 3
    for item in snap["items"]:
        assert item["in_range"] is True
        assert item["limit_basis"]["limit_id"]


def test_publish_unqualified_exclusive_max(client):
    sid = _register(client)
    # max=1.0 为开端：1.0 即超限
    _readings_ok(client, sid, {"turbidity": 1.0, "ph": 7.2, "residual_chlorine": 0.8})
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": 3})
    assert r.status_code == 201
    snap = r.json()
    assert snap["conclusion"] == "unqualified"
    turb = [i for i in snap["items"] if i["indicator_code"] == "turbidity"][0]
    assert turb["in_range"] is False
    assert any(">= max limit" in x for x in turb["violations"])
    assert snap["violations"][0]["indicator_code"] == "turbidity"


def test_publish_unqualified_min_inclusive(client):
    sid = _register(client, partition="P2")
    # P2 余氯 [0.05, 4.0)：0.05 恰好合格，0.04 不合格
    _readings_ok(client, sid, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.04})
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": 3})
    snap = r.json()
    assert snap["conclusion"] == "unqualified"
    cl = [i for i in snap["items"] if i["indicator_code"] == "residual_chlorine"][0]
    assert any("< min limit" in x for x in cl["violations"])


def test_duplicate_publish_rejected_and_republish_after_correction(client):
    sid = _register(client)
    _readings_ok(client, sid, {"turbidity": 1.0, "ph": 7.2, "residual_chlorine": 0.8})
    first = client.post(f"/samples/{sid}/publish", json={"expected_version": 3})
    assert first.status_code == 201
    rid_v3 = first.json()["report_id"]

    again = client.post(f"/samples/{sid}/publish", json={"expected_version": 3})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_published"

    # 更正浑浊度，版本前进到 4，再发布 -> 新版本合格报告，旧版仍可查
    _put(client, sid, "turbidity", 0.5, 3, request_no="FIX-TURB-1")
    second = client.post(f"/samples/{sid}/publish", json={"expected_version": 4})
    assert second.status_code == 201
    assert second.json()["conclusion"] == "qualified"
    assert second.json()["report_id"] != rid_v3

    reports = client.get(f"/samples/{sid}/reports").json()["reports"]
    assert [r["reading_version"] for r in reports] == [3, 4]
    assert [r["conclusion"] for r in reports] == ["unqualified", "qualified"]

    old = client.get(f"/samples/{sid}/reports/{rid_v3}").json()
    assert old["conclusion"] == "unqualified"
    assert old["reading_version"] == 3  # 旧快照不被覆盖


def test_concurrent_publish_only_one_succeeds(client):
    sid = _register(client)
    _readings_ok(client, sid, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})

    results: list[int] = []
    errors: list[str] = []
    barrier = threading.Barrier(5)

    def worker():
        try:
            barrier.wait()
            r = client.post(f"/samples/{sid}/publish", json={"expected_version": 3})
            results.append(r.status_code)
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert sorted(results).count(201) == 1
    assert sorted(results).count(409) == 4
    # 库里只有一条报告（无部分报告）
    reports = client.get(f"/samples/{sid}/reports").json()["reports"]
    assert len(reports) == 1


def test_failed_publish_leaves_no_report(client):
    sid = _register(client)
    _readings_ok(client, sid, {"ph": 7.1, "turbidity": 0.2})  # 缺余氯
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": 2})
    assert r.status_code == 422
    assert client.get(f"/samples/{sid}/reports").json()["reports"] == []


def test_publish_with_stale_version(client):
    sid = _register(client)
    _readings_ok(client, sid, {"turbidity": 0.2, "ph": 7.2, "residual_chlorine": 0.8})
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": 2})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "version_conflict"
    assert client.get(f"/samples/{sid}/reports").json()["reports"] == []
