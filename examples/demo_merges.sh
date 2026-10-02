#!/usr/bin/env bash
# 过敏原代码并码专项演示：
#   登记并码（旧代码 -> 目标代码，必填原因） -> 串接 -> 代码查询沿革 ->
#   实时溯源按最终规范代码合并同义项/去重（保留每条原始来源路径与声明版本） ->
#   标签快照保留原值 -> 变更事件流 -> 拒绝场景（自映射/别名目标/改指/幂等重试） ->
#   撤销单条并码（记录号 + 必填原因；串接链按仍生效关系解析；列表可查原登记与
#   撤销原因/时间；撤销后旧代码可重新并入当前非别名目标；非法再并码拒绝）
# 用法：BASE=http://localhost:8000 ./examples/demo_merges.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"

step() { echo -e "\n\033[1;36m== $* ==\033[0m"; }
req() { # METHOD PATH JSON
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo
}

step "1. 登记原料（原始过敏原代码：casein / dairy / milk）与加工产出"
req POST /materials '{"batch_id":"RM-A","allergens":["casein"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-B","allergens":["dairy"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /materials '{"batch_id":"RM-C","allergens":["milk"],"declared_at":"2026-01-02T06:00:00Z"}'
req POST /processings '{"processing_id":"P1","equipment_id":"EQ-9","start_utc":"2026-01-02T08:00:00Z","end_utc":"2026-01-02T09:00:00Z","inputs":["RM-A","RM-B","RM-C"],"outputs":["OUT-9"]}'

step "2. 并码前签发标签（冻结当时结论：三个代码各自独立）"
req POST /batches/OUT-9/labels '{}'

step "3. 登记并码（必填原因）：casein -> dairy，再串接 dairy -> milk"
req POST /allergen-merges '{"old_code":"casein","target_code":"dairy","reason":"供应商代码勘误：酪蛋白归入乳制品类"}'
req POST /allergen-merges '{"old_code":"dairy","target_code":"milk","reason":"标准 GB-xxxx 统一代码：dairy 并入 milk"}'

step "4. 查询代码：最终规范代码 + 并码沿革（串接链每一跳）"
req GET /allergen-codes/casein
req GET /allergen-codes/milk
req GET /allergen-merges

step "5. 实时溯源：同义项合并为 milk 并去重，保留每条原始来源路径与声明版本；待确认状态不受影响"
req GET /batches/OUT-9/trace

step "6. 已签发标签快照保留原值（casein/dairy/milk 原样不变）"
req GET /labels/1

step "7. 并码事件流（沿用既有字段与全局顺序，仅实际变化批次；op=allergen_code_merge）"
CURSOR=$(curl -sS "$BASE/changes/snapshot" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')
req GET "/changes/events?cursor=0&limit=20"

step "8. 拒绝场景：自映射 409；目标当前是别名 409；同一旧代码改指 409；原因空白 422"
req POST /allergen-merges '{"old_code":"milk","target_code":"milk","reason":"x"}'
req POST /allergen-merges '{"old_code":"milk","target_code":"casein","reason":"x"}'
req POST /allergen-merges '{"old_code":"casein","target_code":"milk","reason":"改指其他目标"}'
req POST /allergen-merges '{"old_code":"casein","target_code":"dairy","reason":"   "}'

step "9. 同向重试：完全相同的并码请求幂等返回原登记（200，replayed=true，不重复写/不产事件）"
req POST /allergen-merges '{"old_code":"dairy","target_code":"milk","reason":"重复提交同样请求"}'

step "10. 并码后新原料登记/声明更正按当前并码关系解释（存原始代码，实时出规范代码）"
req POST /materials '{"batch_id":"RM-D","allergens":["casein"],"declared_at":"2026-01-03T06:00:00Z"}'
req GET /batches/RM-D/trace

# ---- 并码撤销（质控复核发现登记有误）----
step "11. 撤销单条关系：凭并码记录号 + 必填原因（取 dairy->milk 的记录号）"
MID_DAIRY=$(curl -sS "$BASE/allergen-merges" | python3 -c '
import sys,json
merges=json.load(sys.stdin)["merges"]
print(next(m["merge_id"] for m in merges if m["old_code"]=="dairy"))')
echo "merge_id(dairy->milk) = $MID_DAIRY"
req POST "/allergen-merges/$MID_DAIRY/revoke" '{"reason":"质控复核：dairy 不应并入 milk，登记撤销"}'

step "12. 串接链按仍生效关系解析：撤销 dairy->milk 后 casein 归 dairy（不再到 milk）"
req GET /allergen-codes/casein
req GET /allergen-codes/dairy
req GET /batches/OUT-9/trace

step "13. 并码列表可查原登记及撤销原因、时间（?status=revoked 只看已撤销）"
req GET /allergen-merges
req GET "/allergen-merges?status=revoked"

step "14. 撤销后旧代码可重新并入当前非别名目标（另起新记录，原撤销记录保留）"
# 先撤销仍生效的 casein->dairy（此时 dairy 已是规范代码、非别名）
MID_CASEIN=$(curl -sS "$BASE/allergen-merges?status=active" | python3 -c '
import sys,json
merges=json.load(sys.stdin)["merges"]
print(next(m["merge_id"] for m in merges if m["old_code"]=="casein"))')
req POST "/allergen-merges/$MID_CASEIN/revoke" '{"reason":"质控复核：casein 登记亦需重核"}'
req GET /allergen-codes/casein   # 撤销后以自身为规范代码
# 再把 casein 重新并入当前非别名目标 dairy：新登记 201（merge_id 与旧记录不同）
req POST /allergen-merges '{"old_code":"casein","target_code":"dairy","reason":"复核后确认 casein 归 dairy（重新并码，新记录）"}'
# 对“仍生效”关系的同向重试依旧幂等（200，replayed=true）
req POST /allergen-merges '{"old_code":"casein","target_code":"dairy","reason":"重复提交同样请求"}'
req GET /allergen-codes/casein

step "15. 撤销的拒绝场景：未知记录 404 / 重复撤销 409 / 空白原因 422"
req POST "/allergen-merges/$MID_DAIRY/revoke" '{"reason":"再次撤销"}'
req POST /allergen-merges/999999/revoke '{"reason":"未知记录"}'
req POST "/allergen-merges/$MID_DAIRY/revoke" '{"reason":"   "}'
# 撤销使未提交的预演凭据失效（预演期间发生并码撤销，提交时 409 需重新预演）
PREV=$(req POST /materials/RM-A/declarations/preview '{"allergens":["casein"]}')
echo "$PREV" | python3 -c 'import sys,json;print("preview_id =",json.load(sys.stdin)["preview_id"])'
