"""业务规则端到端测试."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

FROM, TO = "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"
SAMPLED_AT = "2026-06-01T10:00:00Z"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    return TestClient(app)


def make_zone(client: TestClient, zone="Z1", indicator="turbidity", max_value=1.0,
              frm=FROM, to=TO):
    assert client.post("/zones", json={"name": zone}).status_code == 201
    assert client.put(f"/zones/{zone}/indicators/{indicator}").status_code == 200
    r = client.post(f"/zones/{zone}/indicators/{indicator}/limits",
                    json={"effective_from": frm, "effective_to": to,
                          "max_value": max_value})
    assert r.status_code == 201, r.text
    return r.json()["rule_id"]


def make_sample(client: TestClient, zone="Z1", sampled_at=SAMPLED_AT) -> int:
    r = client.post("/samples", json={"zone": zone, "sampled_at": sampled_at})
    assert r.status_code == 201, r.text
    return r.json()["sample_id"]


# ---------------------------------------------------------------- 限值区间

def test_invalid_interval_rejected(client):
    client.post("/zones", json={"name": "Z1"})
    client.put("/zones/Z1/indicators/turbidity")
    r = client.post("/zones/Z1/indicators/turbidity/limits",
                    json={"effective_from": TO, "effective_to": FROM, "max_value": 1.0})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_interval"
    # 起点 == 终点也无效
    r = client.post("/zones/Z1/indicators/turbidity/limits",
                    json={"effective_from": FROM, "effective_to": FROM, "max_value": 1.0})
    assert r.status_code == 422


def test_overlapping_interval_rejected_adjacent_allowed(client):
    client.post("/zones", json={"name": "Z1"})
    client.put("/zones/Z1/indicators/turbidity")
    ok = client.post("/zones/Z1/indicators/turbidity/limits",
                     json={"effective_from": FROM, "effective_to": "2026-07-01T00:00:00Z",
                           "max_value": 1.0})
    assert ok.status_code == 201
    # 重叠: 部分覆盖
    r = client.post("/zones/Z1/indicators/turbidity/limits",
                    json={"effective_from": "2026-06-01T00:00:00Z",
                          "effective_to": TO, "max_value": 2.0})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "interval_overlap"
    # 重叠: 被包含
    r = client.post("/zones/Z1/indicators/turbidity/limits",
                    json={"effective_from": "2026-02-01T00:00:00Z",
                          "effective_to": "2026-03-01T00:00:00Z", "max_value": 2.0})
    assert r.status_code == 409
    # 相邻(左闭右开, 端点相接)不算重叠
    r = client.post("/zones/Z1/indicators/turbidity/limits",
                    json={"effective_from": "2026-07-01T00:00:00Z",
                          "effective_to": TO, "max_value": 2.0})
    assert r.status_code == 201
    # 不同分区/不同指标互不影响
    client.post("/zones", json={"name": "Z2"})
    r = client.post("/zones/Z2/indicators/turbidity/limits",
                    json={"effective_from": FROM, "effective_to": TO, "max_value": 5.0})
    assert r.status_code == 201


# ---------------------------------------------------------------- 读数录入/更正

def test_reading_entry_and_version_conflict(client):
    make_zone(client)
    sid = make_sample(client)
    # 版本不匹配 -> 409
    r = client.post(f"/samples/{sid}/readings",
                    json={"indicator": "turbidity", "value": 0.5, "expected_version": 3})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "version_conflict"
    # 正确版本 -> 成功, 版本 +1
    r = client.post(f"/samples/{sid}/readings",
                    json={"indicator": "turbidity", "value": 0.5, "expected_version": 0})
    assert r.status_code == 201
    assert r.json()["version"] == 1
    # 重复录入 -> 409
    r = client.post(f"/samples/{sid}/readings",
                    json={"indicator": "turbidity", "value": 0.6, "expected_version": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "reading_exists"
    # 负数读数 -> 422
    r = client.post(f"/samples/{sid}/readings",
                    json={"indicator": "ph", "value": -0.1, "expected_version": 1})
    assert r.status_code == 422


def test_correction_idempotency(client):
    make_zone(client)
    sid = make_sample(client)
    client.post(f"/samples/{sid}/readings",
                json={"indicator": "turbidity", "value": 0.5, "expected_version": 0})
    body = {"value": 0.8, "expected_version": 1, "request_id": "req-001"}
    r1 = client.put(f"/samples/{sid}/readings/turbidity", json=body)
    assert r1.status_code == 200
    assert r1.json()["version"] == 2
    # 重放: 同号同内容 -> 返回原结果, 版本不再前进
    r2 = client.put(f"/samples/{sid}/readings/turbidity", json=body)
    assert r2.status_code == 200
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r2.json() == r1.json()
    assert client.get(f"/samples/{sid}").json()["version"] == 2
    # 同号异内容 -> 409
    r3 = client.put(f"/samples/{sid}/readings/turbidity",
                    json={"value": 0.9, "expected_version": 2, "request_id": "req-001"})
    assert r3.status_code == 409
    assert r3.json()["detail"]["error"] == "request_id_conflict"
    # 过期版本 -> 409
    r4 = client.put(f"/samples/{sid}/readings/turbidity",
                    json={"value": 0.9, "expected_version": 1, "request_id": "req-002"})
    assert r4.status_code == 409
    assert r4.json()["detail"]["error"] == "version_conflict"
    # 更正不存在的读数 -> 404
    r5 = client.put(f"/samples/{sid}/readings/ph",
                    json={"value": 7.0, "expected_version": 2, "request_id": "req-003"})
    assert r5.status_code == 404


# ---------------------------------------------------------------- 发布

def test_publish_requires_complete_readings_and_limits(client):
    client.post("/zones", json={"name": "Z1"})
    client.put("/zones/Z1/indicators/turbidity")
    client.put("/zones/Z1/indicators/ph")
    client.post("/zones/Z1/indicators/turbidity/limits",
                json={"effective_from": FROM, "effective_to": TO, "max_value": 1.0})
    sid = make_sample(client)
    # 读数全无 -> 422 missing
    r = client.post(f"/samples/{sid}/publish")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "missing_readings"
    client.post(f"/samples/{sid}/readings",
                json={"indicator": "turbidity", "value": 0.5, "expected_version": 0})
    client.post(f"/samples/{sid}/readings",
                json={"indicator": "ph", "value": 7.0, "expected_version": 1})
    # ph 没有生效限值 -> 422
    r = client.post(f"/samples/{sid}/publish")
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["error"] == "limit_not_exactly_one"
    assert body["problems"] == [{"indicator": "ph", "effective_limit_count": 0}]
    # 失败后不得留下部分报告
    assert client.get(f"/samples/{sid}/reports").json()["reports"] == []
    # 采样时刻不在限值区间内 -> 同样拒绝
    client.post("/zones/Z1/indicators/ph/limits",
                json={"effective_from": "2026-07-01T00:00:00Z", "effective_to": TO,
                      "max_value": 9.0})
    r = client.post(f"/samples/{sid}/publish")
    assert r.status_code == 422
    # 补齐采样时刻的限值 -> 发布成功
    client.post("/zones/Z1/indicators/ph/limits",
                json={"effective_from": FROM, "effective_to": "2026-07-01T00:00:00Z",
                      "max_value": 9.0})
    r = client.post(f"/samples/{sid}/publish")
    assert r.status_code == 201, r.text
    assert r.json()["conclusion"] == "compliant"


def test_publish_snapshot_and_versioning(client):
    make_zone(client, max_value=1.0)
    sid = make_sample(client)
    client.post(f"/samples/{sid}/readings",
                json={"indicator": "turbidity", "value": 0.5, "expected_version": 0})
    r = client.post(f"/samples/{sid}/publish")
    assert r.status_code == 201
    rep1 = r.json()
    assert rep1["conclusion"] == "compliant"
    assert rep1["report_version"] == 1
    assert rep1["sample_version"] == 1
    assert rep1["items"][0]["limit"]["max_value"] == 1.0
    assert rep1["items"][0]["exceeded"] is False
    # 同一读数版本重复发布 -> 409
    r = client.post(f"/samples/{sid}/publish")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "already_published"
    # 更正为超限读数后再发布 -> 新报告版本, 结论不合格
    client.put(f"/samples/{sid}/readings/turbidity",
               json={"value": 2.5, "expected_version": 1, "request_id": "fix-1"})
    r = client.post(f"/samples/{sid}/publish")
    assert r.status_code == 201
    rep2 = r.json()
    assert rep2["report_version"] == 2
    assert rep2["sample_version"] == 2
    assert rep2["conclusion"] == "non_compliant"
    assert rep2["items"][0]["exceeded"] is True
    # 旧版本仍可查询且未被覆盖
    r = client.get(f"/samples/{sid}/reports/1")
    assert r.status_code == 200
    assert r.json()["conclusion"] == "compliant"
    assert r.json()["items"][0]["value"] == 0.5
    versions = client.get(f"/samples/{sid}/reports").json()["reports"]
    assert [v["report_version"] for v in versions] == [1, 2]


def test_concurrent_publish_only_one_succeeds(client):
    make_zone(client)
    sid = make_sample(client)
    client.post(f"/samples/{sid}/readings",
                json={"indicator": "turbidity", "value": 0.5, "expected_version": 0})

    results: list[int] = []

    def do_publish():
        c = TestClient(client.app)
        results.append(c.post(f"/samples/{sid}/publish").status_code)

    threads = [threading.Thread(target=do_publish) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 409, 409, 409]
    reports = client.get(f"/samples/{sid}/reports").json()["reports"]
    assert len(reports) == 1
