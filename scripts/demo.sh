#!/usr/bin/env bash
# 端到端演示：迟到改站重算、幂等重传、冲突拒收、时段重叠拒收。
# 用法：先启动服务（docker compose up -d），再执行 scripts/demo.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
SAMPLES="$(cd "$(dirname "$0")/../samples" && pwd)"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 导入刷卡流水 swipes_1.csv（此时无映射，全部未归属）"
curl -s -F "file=@${SAMPLES}/swipes_1.csv" "${BASE}/api/imports/swipes"; echo
curl -s "${BASE}/api/transactions/unattributed" | python3 -m json.tool | head -5

step "2. 导入映射 mappings_1_initial.csv（旧流水立即被归属）"
curl -s -F "file=@${SAMPLES}/mappings_1_initial.csv" "${BASE}/api/imports/mappings"; echo
curl -s "${BASE}/api/stats/monthly" | python3 -m json.tool

step "3. 迟到改站 mappings_2_late_correction.csv（整组替换 L1/T100，3 月窗口延长）"
curl -s -F "file=@${SAMPLES}/mappings_2_late_correction.csv" "${BASE}/api/imports/mappings"; echo
echo "-- 2026-03 统计（S007 被重新归属到 Central Station North Gate）:"
curl -s "${BASE}/api/stats/monthly?month=2026-03" | python3 -m json.tool

step "4. 幂等重传 swipes_2_more.csv（S001 同号同内容 -> 跳过不重复计数）"
curl -s -F "file=@${SAMPLES}/swipes_2_more.csv" "${BASE}/api/imports/swipes"; echo

step "5. 同号不同内容 swipes_3_conflict.csv -> 409 整批拒收（S013 也不入库）"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -F "file=@${SAMPLES}/swipes_3_conflict.csv" "${BASE}/api/imports/swipes"

step "6. 组内时段重叠 mappings_3_overlap.csv -> 409 整批拒收（旧映射不变）"
curl -s -o /dev/null -w "HTTP %{http_code}\n" -F "file=@${SAMPLES}/mappings_3_overlap.csv" "${BASE}/api/imports/mappings"
curl -s "${BASE}/api/mappings?line=L1&temp_station_id=T200" | python3 -m json.tool

step "7. 最终状态"
curl -s "${BASE}/api/stats/monthly" | python3 -m json.tool
curl -s "${BASE}/api/overview"; echo

step "8. 可复核补传 swipes_4_reviewable.csv（S001 冲突待核，S013/S014 立即入库）"
curl -s -F "file=@${SAMPLES}/swipes_4_reviewable.csv" "${BASE}/api/imports/swipes/reviewable"; echo
echo "-- 待核记录（S001 原值 vs 新值）:"
curl -s "${BASE}/api/reviews?status=pending" | python3 -m json.tool

step "9. 审批第一条待核：保留原值"
RID=$(curl -s "${BASE}/api/reviews?status=pending&txn_id=S001" | python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["review_id"])')
curl -s -H 'Content-Type: application/json' -d '{"decision":"keep"}' "${BASE}/api/reviews/${RID}/decision" | python3 -m json.tool

step "10. 最终状态（含待核计数）"
curl -s "${BASE}/api/overview"; echo
