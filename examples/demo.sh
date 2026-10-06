#!/usr/bin/env bash
# 端到端调用示例: 从建分区到发布报告、查询历史版本
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
json() { python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))"; }

echo "== 1. 创建分区 / 必测指标 / 限值区间(左闭右开) =="
curl -s -X POST "$BASE/zones" -H 'Content-Type: application/json' -d '{"name":"东区"}' | json
curl -s -X PUT "$BASE/zones/东区/indicators/turbidity" | json
curl -s -X POST "$BASE/zones/东区/indicators/turbidity/limits" -H 'Content-Type: application/json' -d '{
  "effective_from": "2026-01-01T00:00:00Z",
  "effective_to":   "2027-01-01T00:00:00Z",
  "max_value": 1.0
}' | json

echo "== 2. 重叠区间被拒绝(409 interval_overlap) =="
curl -s -X POST "$BASE/zones/东区/indicators/turbidity/limits" -H 'Content-Type: application/json' -d '{
  "effective_from": "2026-06-01T00:00:00Z",
  "effective_to":   "2026-12-01T00:00:00Z",
  "max_value": 2.0
}' | json

echo "== 3. 登记样本(分区 + 采样时刻) =="
curl -s -X POST "$BASE/samples" -H 'Content-Type: application/json' -d '{
  "zone": "东区", "sampled_at": "2026-10-06T08:30:00Z"
}' | json

echo "== 4. 录入读数(带预期版本 0) =="
curl -s -X POST "$BASE/samples/1/readings" -H 'Content-Type: application/json' -d '{
  "indicator": "turbidity", "value": 0.42, "expected_version": 0
}' | json

echo "== 5. 过期版本被拒绝(409 version_conflict) =="
curl -s -X POST "$BASE/samples/1/readings" -H 'Content-Type: application/json' -d '{
  "indicator": "turbidity", "value": 0.5, "expected_version": 0
}' | json

echo "== 6. 发布 -> 合格报告(report_version=1) =="
curl -s -X POST "$BASE/samples/1/publish" | json

echo "== 7. 同一读数版本重复发布被拒绝(409 already_published) =="
curl -s -X POST "$BASE/samples/1/publish" | json

echo "== 8. 更正读数(幂等请求号) -> 版本前进到 2 =="
curl -s -X PUT "$BASE/samples/1/readings/turbidity" -H 'Content-Type: application/json' -d '{
  "value": 3.2, "expected_version": 1, "request_id": "corr-20261006-001"
}' | json

echo "== 9. 重放同一请求号 -> 返回原结果(响应头 X-Idempotent-Replay: true) =="
curl -s -D - -o /dev/null -X PUT "$BASE/samples/1/readings/turbidity" \
  -H 'Content-Type: application/json' -d '{
  "value": 3.2, "expected_version": 1, "request_id": "corr-20261006-001"
}' | grep -i 'x-idempotent-replay' || true

echo "== 10. 同号异内容 -> 409 request_id_conflict =="
curl -s -X PUT "$BASE/samples/1/readings/turbidity" -H 'Content-Type: application/json' -d '{
  "value": 9.9, "expected_version": 2, "request_id": "corr-20261006-001"
}' | json

echo "== 11. 更正后再发布 -> 新报告版本(report_version=2, 不合格), 旧版不覆盖 =="
curl -s -X POST "$BASE/samples/1/publish" | json

echo "== 12. 查询报告历史与旧版本快照 =="
curl -s "$BASE/samples/1/reports" | json
curl -s "$BASE/samples/1/reports/1" | json
