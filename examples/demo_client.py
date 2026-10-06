#!/usr/bin/env python3
"""自来水厂取样合规复核服务 —— Python 调用示例（仅标准库，无需 pip 安装）。

流程覆盖：
  登记样本 -> 录入读数 -> 版本冲突 -> 更正(请求号/重放/同号异内容)
  -> 首次发布(不合格快照) -> 重复发布拒绝 -> 更正后再发布(新版本合格, 旧版可查)
  -> 读数不齐/无生效限值门禁。

用法：
    python examples/demo_client.py            # 默认 http://localhost:8080
    BASE=http://127.0.0.1:8080 python examples/demo_client.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://localhost:8080").rstrip("/")


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def step(title: str) -> None:
    print(f"\n== {title} " + "=" * max(4, 60 - len(title)))


def expect(status: int, got: int, label: str) -> None:
    flag = "OK " if got == status else "FAIL"
    print(f"  [{flag}] {label}: HTTP {got} (expect {status})")
    if got != status:
        sys.exit(1)


def main() -> None:
    step("健康检查")
    print(" ", call("GET", "/health"))

    step("登记样本（P1，采样时刻 +08:00，服务端归一 UTC）")
    s, sample = call("POST", "/samples",
                     {"partition_code": "P1", "sampled_at": "2025-06-01T08:00:00+08:00"})
    expect(201, s, "register")
    sid = sample["id"]
    print("  sample:", sid, "sampled_at(UTC):", sample["sampled_at"], "version:", sample["reading_version"])

    step("录入 3 项必测读数（浑浊度 1.2 将超 1.0 上限；上限为开端）")
    plan = [("turbidity", 1.2), ("ph", 7.2), ("residual_chlorine", 0.8)]
    version = 0
    for indicator, value in plan:
        s, r = call("PUT", f"/samples/{sid}/readings/{indicator}",
                    {"indicator_code": indicator, "value": value, "expected_version": version})
        expect(200, s, f"entry {indicator}")
        print("  ", r)
        version = r["reading_version"]

    step("用过期版本写入 -> 409 version_conflict")
    s, r = call("PUT", f"/samples/{sid}/readings/ph",
                {"indicator_code": "ph", "value": 7.9, "expected_version": 0})
    expect(409, s, "stale version")
    print("  current_version returned:", r["error"]["details"]["current_version"])

    step("更正已有读数但不带请求号 -> 422")
    s, r = call("PUT", f"/samples/{sid}/readings/turbidity",
                {"indicator_code": "turbidity", "value": 0.5, "expected_version": version})
    expect(422, s, "correction without request_no")
    print("  code:", r["error"]["code"])

    step("带唯一请求号 REQ-42 更正浑浊度 -> 版本 +1")
    payload = {"indicator_code": "turbidity", "value": 0.5,
               "expected_version": version, "request_no": "REQ-42"}
    s, first = call("PUT", f"/samples/{sid}/readings/turbidity", payload)
    expect(200, s, "correction")
    print("  ", first)
    version = first["reading_version"]

    step("同号重放 -> 原结果，版本不再增加")
    s, replay = call("PUT", f"/samples/{sid}/readings/turbidity", payload)
    expect(200, s, "replay")
    assert replay == first, (first, replay)
    print("   replay result identical:", replay)

    step("同号异内容（值不同） -> 422 idempotency_key_conflict")
    s, r = call("PUT", f"/samples/{sid}/readings/turbidity",
                {**payload, "value": 0.9})
    expect(422, s, "same request_no different payload")
    print("  code:", r["error"]["code"])

    step("发布报告（当前浑浊度 0.5 合格）")
    s, report = call("POST", f"/samples/{sid}/publish", {"expected_version": version})
    expect(201, s, "publish v4 qualified")
    assert report["conclusion"] == "qualified"
    print("  conclusion:", report["conclusion"], "| items:",
          [(i["indicator_code"], i["value"], i["in_range"],
            (i["limit_basis"]["min_value"], i["limit_basis"]["max_value"]))
           for i in report["items"]])
    report_id = report["report_id"]

    step("同一读数版本重复发布 -> 409 already_published")
    s, r = call("POST", f"/samples/{sid}/publish", {"expected_version": version})
    expect(409, s, "duplicate publish")
    print("  code:", r["error"]["code"])

    step("再更正为超限值 1.0（max 开端，等于上限即超限）并发布 -> 新版本不合格")
    s, corr = call("PUT", f"/samples/{sid}/readings/turbidity",
                   {"indicator_code": "turbidity", "value": 1.0,
                    "expected_version": version, "request_no": "REQ-43"})
    expect(200, s, "second correction")
    version = corr["reading_version"]
    s, report2 = call("POST", f"/samples/{sid}/publish", {"expected_version": version})
    expect(201, s, "publish new version")
    assert report2["conclusion"] == "unqualified"
    print("  conclusion:", report2["conclusion"], "| violations:", report2["violations"])

    step("报告列表：两个读数版本并存，旧快照不被覆盖")
    s, listing = call("GET", f"/samples/{sid}/reports")
    expect(200, s, "list reports")
    for item in listing["reports"]:
        print("  ", item)
    s, old = call("GET", f"/samples/{sid}/reports/{report_id}")
    expect(200, s, "fetch old report")
    assert old["conclusion"] == "qualified" and old["reading_version"] == 4
    print(f"  old report v{old['reading_version']} still:", old["conclusion"])

    step("门禁：读数不齐 -> 422 readings_incomplete")
    _, bad = call("POST", "/samples", {"partition_code": "P1", "sampled_at": "2025-06-01T00:00:00Z"})
    call("PUT", f"/samples/{bad['id']}/readings/ph",
         {"indicator_code": "ph", "value": 7.2, "expected_version": 0})
    s, r = call("POST", f"/samples/{bad['id']}/publish", {"expected_version": 1})
    expect(422, s, "incomplete readings")
    print("  missing:", r["error"]["details"]["missing_indicators"])

    step("门禁：采样时刻无生效限值 -> 422 no_effective_limit")
    _, nolim = call("POST", "/samples",
                    {"partition_code": "P1", "sampled_at": "2024-12-31T16:00:00Z"})
    for i, (indicator, value) in enumerate(plan):
        call("PUT", f"/samples/{nolim['id']}/readings/{indicator}",
             {"indicator_code": indicator, "value": value, "expected_version": i})
    s, r = call("POST", f"/samples/{nolim['id']}/publish", {"expected_version": 3})
    expect(422, s, "no effective limit")
    print("  indicators without limit:", r["error"]["details"]["indicators_without_limit"])

    step("区间维护：重叠区间被拒绝 / 端点相接允许")
    s, r = call("POST", "/admin/partitions/P1/limits",
                {"indicator_code": "turbidity", "max_value": 2.0,
                 "valid_from": "2025-06-01T00:00:00Z", "valid_to": "2025-07-01T00:00:00Z"})
    expect(422, s, "overlap rejected")
    print("  code:", r["error"]["code"])
    s, r = call("POST", "/admin/partitions/P2/limits",
                {"indicator_code": "residual_chlorine", "min_value": 0.03, "max_value": 4.0,
                 "valid_from": "2026-01-01T00:00:00Z", "valid_to": "2027-01-01T00:00:00Z"})
    expect(201, s, "adjacent interval accepted")

    print("\nALL DEMO STEPS PASSED")


if __name__ == "__main__":
    main()
