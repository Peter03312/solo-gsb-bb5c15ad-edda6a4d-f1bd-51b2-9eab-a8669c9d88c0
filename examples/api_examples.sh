#!/usr/bin/env bash
# 自来水厂取样合规复核服务 —— curl 调用示例（完整业务流）
# 依赖：bash、curl、python3（仅用于解析 JSON）
#
# 用法：
#   BASE=http://localhost:8080 bash examples/api_examples.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"
SAMPLED_AT="2025-06-01T08:00:00+08:00"   # 种子限值 2025 年生效，命中唯一一条
SAMPLED_AT_UTC="2025-06-01T00:00:00Z"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
jqget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval(\"d$1\"))"; }

say "0. 健康检查"
curl -sS "$BASE/health"; echo

say "1. 查看分区 P1 的必测指标与生效限值（启动种子数据）"
curl -sS "$BASE/admin/partitions/P1" | python3 -m json.tool

say "2. 维护主数据：新建指标 / 分区 / 必测关系 / 限值区间"
curl -sS -X POST "$BASE/admin/indicators" -H 'Content-Type: application/json' \
  -d '{"code":"conductivity","name":"电导率","unit":"uS/cm"}' | python3 -m json.tool
curl -sS -X POST "$BASE/admin/partitions" -H 'Content-Type: application/json' \
  -d '{"code":"P3","name":"管网水三号分区"}' | python3 -m json.tool
curl -sS -X POST "$BASE/admin/partitions/P3/mandatory-indicators" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"conductivity"}' | python3 -m json.tool
curl -sS -X POST "$BASE/admin/partitions/P3/limits" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"conductivity","max_value":300.0,
       "valid_from":"2025-01-01T00:00:00Z","valid_to":"2026-01-01T00:00:00Z"}' | python3 -m json.tool

say "2b. 区间重叠被拒绝（422 limit_interval_overlap）"
curl -sS -o /tmp/ov.json -w 'HTTP %{http_code}\n' -X POST "$BASE/admin/partitions/P3/limits" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"conductivity","max_value":400.0,
       "valid_from":"2025-12-01T00:00:00Z","valid_to":"2026-06-01T00:00:00Z"}'
cat /tmp/ov.json | python3 -m json.tool

say "3. 登记样本（分区 + 采样时刻，时刻可带时区偏移，服务端归一 UTC）"
SAMPLE_JSON=$(curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' \
  -d "{\"partition_code\":\"P1\",\"sampled_at\":\"$SAMPLED_AT\"}")
echo "$SAMPLE_JSON" | python3 -m json.tool
SID=$(echo "$SAMPLE_JSON" | jqget "['id']")
echo "sample id = $SID"

say "4. 录入读数（非负 + expected_version；初始版本为 0，每次成功写入版本 +1）"
curl -sS -X PUT "$BASE/samples/$SID/readings/turbidity" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"turbidity","value":1.2,"expected_version":0}' | python3 -m json.tool
curl -sS -X PUT "$BASE/samples/$SID/readings/ph" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"ph","value":7.2,"expected_version":1}' | python3 -m json.tool
curl -sS -X PUT "$BASE/samples/$SID/readings/residual_chlorine" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"residual_chlorine","value":0.8,"expected_version":2}' | python3 -m json.tool

say "4b. 版本过期 -> 409 version_conflict"
curl -sS -o /tmp/conf.json -w 'HTTP %{http_code}\n' -X PUT "$BASE/samples/$SID/readings/ph" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"ph","value":7.9,"expected_version":0}'
cat /tmp/conf.json | python3 -m json.tool

say "5. 更正读数必须带唯一请求号（不带 -> 422 correction_requires_request_no）"
curl -sS -o /tmp/c0.json -w 'HTTP %{http_code}\n' -X PUT "$BASE/samples/$SID/readings/turbidity" \
  -H 'Content-Type: application/json' \
  -d '{"indicator_code":"turbidity","value":0.5,"expected_version":3}'
cat /tmp/c0.json | python3 -m json.tool

say "5b. 带请求号更正成功（版本 3 -> 4）"
REQNO="fix-turb-$(date +%s)"
curl -sS -X PUT "$BASE/samples/$SID/readings/turbidity" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"turbidity\",\"value\":0.5,\"expected_version\":3,\"request_no\":\"$REQNO\"}" \
  | python3 -m json.tool

say "5c. 同号重放 -> 返回原结果，版本不再变化"
curl -sS -X PUT "$BASE/samples/$SID/readings/turbidity" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"turbidity\",\"value\":0.5,\"expected_version\":3,\"request_no\":\"$REQNO\"}" \
  | python3 -m json.tool

say "5d. 同号异内容 -> 422 idempotency_key_conflict"
curl -sS -o /tmp/c1.json -w 'HTTP %{http_code}\n' -X PUT "$BASE/samples/$SID/readings/turbidity" \
  -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"turbidity\",\"value\":0.9,\"expected_version\":3,\"request_no\":\"$REQNO\"}"
cat /tmp/c1.json | python3 -m json.tool

say "6. 发布合规报告（读数齐全 + 每项采样时刻恰有一条生效限值；任一超限不合格）"
curl -sS -X POST "$BASE/samples/$SID/publish" -H 'Content-Type: application/json' \
  -d '{"expected_version":4}' | python3 -m json.tool

say "6b. 同一读数版本重复发布 -> 409 already_published"
curl -sS -o /tmp/p1.json -w 'HTTP %{http_code}\n' -X POST "$BASE/samples/$SID/publish" \
  -H 'Content-Type: application/json' -d '{"expected_version":4}'
cat /tmp/p1.json | python3 -m json.tool

say "7. 更正后再发布 -> 新版本报告，旧版保留可查"
REQNO2="fix-turb2-$(date +%s)"
curl -sS -X PUT "$BASE/samples/$SID/readings/turbidity" -H 'Content-Type: application/json' \
  -d "{\"indicator_code\":\"turbidity\",\"value\":0.3,\"expected_version\":4,\"request_no\":\"$REQNO2\"}" \
  | python3 -m json.tool
curl -sS -X POST "$BASE/samples/$SID/publish" -H 'Content-Type: application/json' \
  -d '{"expected_version":5}' | python3 -m json.tool

say "7b. 报告列表（应含两个读数版本）"
curl -sS "$BASE/samples/$SID/reports" | python3 -m json.tool

say "8. 查询样本（当前读数、版本、已发布报告摘要）"
curl -sS "$BASE/samples/$SID" | python3 -m json.tool

say "9. 门禁：读数不齐禁止发布"
BAD=$(curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' \
  -d "{\"partition_code\":\"P1\",\"sampled_at\":\"$SAMPLED_AT_UTC\"}" | jqget "['id']")
curl -sS -X PUT "$BASE/samples/$BAD/readings/ph" -H 'Content-Type: application/json' \
  -d '{"indicator_code":"ph","value":7.2,"expected_version":0}' >/dev/null
curl -sS -o /tmp/p2.json -w 'HTTP %{http_code}\n' -X POST "$BASE/samples/$BAD/publish" \
  -H 'Content-Type: application/json' -d '{"expected_version":1}'
cat /tmp/p2.json | python3 -m json.tool

say "10. 门禁：采样时刻无生效限值禁止发布（2024 年采样，限值 2025 年才生效）"
NOLIM=$(curl -sS -X POST "$BASE/samples" -H 'Content-Type: application/json' \
  -d '{"partition_code":"P1","sampled_at":"2024-12-15T08:00:00+08:00"}' | jqget "['id']")
for pair in "turbidity:0.2:0" "ph:7.2:1" "residual_chlorine:0.8:2"; do
  IFS=: read -r ind val ver <<< "$pair"
  curl -sS -X PUT "$BASE/samples/$NOLIM/readings/$ind" -H 'Content-Type: application/json' \
    -d "{\"indicator_code\":\"$ind\",\"value\":$val,\"expected_version\":$ver}" >/dev/null
done
curl -sS -o /tmp/p3.json -w 'HTTP %{http_code}\n' -X POST "$BASE/samples/$NOLIM/publish" \
  -H 'Content-Type: application/json' -d '{"expected_version":3}'
cat /tmp/p3.json | python3 -m json.tool

echo
echo "DONE. 可交互式查看 OpenAPI 文档： $BASE/docs"
