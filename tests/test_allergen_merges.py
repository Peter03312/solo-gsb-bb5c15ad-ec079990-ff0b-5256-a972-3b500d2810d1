"""过敏原代码并码端到端测试。

质控把旧过敏原代码并入目标代码（必填原因）：并码可串接；目标须非空且当前
不是别名；自映射、成环、同一旧代码改指其他目标一律拒绝且不留记录；同向重试
幂等返回原结果（仅对仍生效的关系）。质控发现登记有误可凭并码记录号 + 必填
原因撤销单条关系：串接链按仍生效的关系解析，撤销后旧代码可重新并入当前非
别名目标（另起新记录，原撤销记录保留可查：原登记、撤销原因/时间）。原料
声明版本与已签发标签快照保留原值；实时溯源按最终规范代码合并同义项并去重，
同时保留每条原始来源路径、声明版本，待确认状态不变；并码登记/撤销后重算
受影响批次（含设备残留下游），沿用既有事件字段与全局顺序，仅为实际变化
记录事件（历史事件不可改写）；撤销使未提交的预演凭据失效；既有接口字段与
路径结构保持兼容。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="allergen-merge-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    for t in ["material_declarations", "material_batches", "processing_inputs",
              "processing_outputs", "processings", "cleanings", "labels",
              "trace_events", "investigations", "allergen_code_merges"]:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'trace_events'")
    conn.execute("DELETE FROM sqlite_sequence WHERE name = 'allergen_code_merges'")
    conn.close()
    with TestClient(app) as c:
        yield c


def material(c, batch_id, allergens=None, pending=False,
             declared_at="2026-01-01T00:00:00Z"):
    return c.post("/materials", json={
        "batch_id": batch_id, "allergens": allergens or [],
        "pending": pending, "declared_at": declared_at})


def processing(c, pid, eq, start, end, inputs, outputs):
    return c.post("/processings", json={
        "processing_id": pid, "equipment_id": eq,
        "start_utc": start, "end_utc": end,
        "inputs": inputs, "outputs": outputs})


def merge(c, old, target, reason="同义词并码"):
    return c.post("/allergen-merges", json={
        "old_code": old, "target_code": target, "reason": reason})


def revoke(c, merge_id, reason="质控复核撤销"):
    return c.post(f"/allergen-merges/{merge_id}/revoke",
                  json={"reason": reason})


# ---------------------------------------------------------------- 登记校验

def test_merge_requires_declared_old_code(client):
    material(client, "RM-1", allergens=["milk"])
    r = merge(client, "casein", "milk")
    assert r.status_code == 400  # 旧代码未在任何现存声明中出现
    assert client.get("/allergen-merges").json()["merges"] == []


def test_merge_self_map_rejected_without_record(client):
    material(client, "RM-1", allergens=["milk"])
    r = merge(client, "milk", "milk")
    assert r.status_code == 409
    assert "自映射" in r.json()["detail"]
    assert client.get("/allergen-merges").json()["merges"] == []


def test_merge_target_cannot_be_alias(client):
    material(client, "RM-1", allergens=["milk", "dairy", "casein"])
    assert merge(client, "casein", "milk").status_code == 201
    # milk 当前是规范代码；casein 已是别名，不能再作目标
    r = merge(client, "milk", "casein")
    assert r.status_code == 409
    assert "别名" in r.json()["detail"]
    # 库内仍只有第一条并码
    merges = client.get("/allergen-merges").json()["merges"]
    assert [(m["old_code"], m["target_code"]) for m in merges] == [
        ("casein", "milk")]


def test_same_old_code_cannot_repoint_elsewhere(client):
    material(client, "RM-1", allergens=["milk", "casein", "dairy"])
    assert merge(client, "casein", "milk").status_code == 201
    r = merge(client, "casein", "dairy")
    assert r.status_code == 409
    merges = client.get("/allergen-merges").json()["merges"]
    assert [m["target_code"] for m in merges] == ["milk"]


def test_merge_requires_reason(client):
    material(client, "RM-1", allergens=["milk", "casein"])
    r = client.post("/allergen-merges",
                    json={"old_code": "casein", "target_code": "milk",
                          "reason": "   "})
    assert r.status_code == 422
    assert client.get("/allergen-merges").json()["merges"] == []


def test_idempotent_same_direction_retry_returns_original(client):
    material(client, "RM-1", allergens=["milk", "casein"])
    first = merge(client, "casein", "milk", reason="首次原因")
    assert first.status_code == 201
    merge_id = first.json()["merge_id"]
    second = merge(client, "casein", "milk", reason="再次请求原因不同")
    assert second.status_code == 200  # 同向重试
    body = second.json()
    assert body["merge_id"] == merge_id       # 返回原登记
    assert body["reason"] == "首次原因"        # 原因不被覆盖
    assert body["replayed"] is True
    assert first.json().get("replayed") is False
    merges = client.get("/allergen-merges").json()["merges"]
    assert len(merges) == 1


# ---------------------------------------------------------------- 串接与代码查询

def test_merge_chains_and_code_lookup(client):
    material(client, "RM-1", allergens=["milk", "casein", "dairy"])
    assert merge(client, "casein", "dairy").status_code == 201
    assert merge(client, "dairy", "milk").status_code == 201  # 串接
    info = client.get("/allergen-codes/casein").json()
    assert info["canonical_code"] == "milk"
    assert info["is_alias"] is True
    assert [(h["old_code"], h["target_code"]) for h in info["history"]] == [
        ("casein", "dairy"), ("dairy", "milk")]
    canon = client.get("/allergen-codes/milk").json()
    assert canon["canonical_code"] == "milk"
    assert canon["history"] == []
    unknown = client.get("/allergen-codes/soy").json()
    assert unknown["canonical_code"] == "soy"
    assert unknown["is_alias"] is False


# ---------------------------------------------------------------- 实时溯源规范化

def test_trace_merges_synonyms_dedupes_and_keeps_raw_paths(client):
    # 两条原料链最终汇入同一加工产出：旧代码 casein 与规范代码 milk 合并
    material(client, "RM-A", allergens=["casein"])
    material(client, "RM-B", allergens=["milk"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-A", "RM-B"], ["OUT-1"])
    assert merge(client, "casein", "milk").status_code == 201

    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == ["milk"]              # 同义项合并
    paths = t["allergen_paths"]["milk"]
    # 每条原始来源路径保留（RM-A 上的原始代码 casein、RM-B 上的 milk）
    root_steps = sorted(paths, key=lambda p: p[0]["batch_id"])
    step_a = next(p[0] for p in root_steps if p[0]["batch_id"] == "RM-A")
    step_b = next(p[0] for p in root_steps if p[0]["batch_id"] == "RM-B")
    assert (step_a["allergen_code"], step_a["canonical_code"]) == (
        "casein", "milk")
    # 规范代码自身的声明步骤保持原结构（不附加代码字段）
    assert "allergen_code" not in step_b and "canonical_code" not in step_b
    versions = {d["batch_id"]: d["version"]
                for d in t["basis"]["material_declarations"]}
    assert versions == {"RM-A": 1, "RM-B": 1}     # 声明版本依据保留
    # 原料端：原始 allergens 保留，附加规范化结果
    ma = client.get("/materials/RM-A").json()
    assert ma["allergens"] == ["casein"]
    assert ma["allergens_canonical"] == ["milk"]


def test_trace_chain_resolution_and_product_dedup(client):
    # 同一原料声明里两个旧代码经串接并到同一规范代码，产品端按完整路径去重
    material(client, "RM-1", allergens=["casein", "dairy"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    merge(client, "casein", "dairy")
    merge(client, "dairy", "milk")
    t = client.get("/batches/OUT-1/trace").json()
    assert t["allergens"] == ["milk"]
    paths = t["allergen_paths"]["milk"]
    # 声明步骤不同（原始代码不同）-> 两条路径都保留，不去掉证据
    assert sorted(p[0]["allergen_code"] for p in paths) == ["casein", "dairy"]


def test_merge_keeps_pending_state(client):
    material(client, "RM-1", allergens=["casein"], pending=True)
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    merge(client, "casein", "milk")
    t = client.get("/batches/OUT-1/trace").json()
    assert t["pending"] is True
    assert t["pending_paths"][0][0]["batch_id"] == "RM-1"
    assert t["allergens"] == ["milk"]


def test_merge_recalculates_equipment_residue_downstream(client):
    material(client, "RM-MILK", allergens=["casein"],
             declared_at="2026-01-02T06:00:00Z")
    material(client, "RM-OAT", allergens=["gluten"],
             declared_at="2026-01-02T06:00:00Z")
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-MILK"], ["OUT-MILK-1"])
    processing(client, "P2", "EQ-1",
               "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
               ["RM-OAT"], ["OUT-OAT-1"])  # 残留继承 casein
    assert merge(client, "casein", "milk").status_code == 201
    t = client.get("/batches/OUT-OAT-1/trace").json()
    assert "milk" in t["allergens"] and "casein" not in t["allergens"]
    # 残留路径保留原始来源信息
    milk_paths = t["allergen_paths"]["milk"]
    residue = [s for p in milk_paths for s in p
               if s.get("via") == "equipment_residue"]
    assert residue and residue[0]["equipment_id"] == "EQ-1"


# ---------------------------------------------------------------- 历史声明与标签快照

def test_merge_covers_allergens_only_in_old_declaration_versions(client):
    material(client, "RM-1", allergens=["casein"])
    # 更正后现行声明不再含 casein，但旧版本里有 -> 仍允许登记并码
    client.post("/materials/RM-1/declarations", json={"allergens": ["milk"]})
    assert merge(client, "casein", "milk").status_code == 201


def test_declaration_versions_and_issued_labels_keep_raw_values(client):
    material(client, "RM-1", allergens=["casein"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    issued = client.post("/batches/OUT-1/labels", json={}).json()
    assert issued["conclusion"]["allergens"] == ["casein"]  # 签发时快照
    merge(client, "casein", "milk")
    label = client.get(f"/labels/{issued['label_id']}").json()
    assert label["conclusion"]["allergens"] == ["casein"]   # 旧标签不变
    frozen_step = label["conclusion"]["allergen_paths"]["casein"][0][0]
    assert "allergen_code" not in frozen_step  # 旧快照结构原样保留
    # 声明历史仍保留原始代码
    decls = client.post(
        "/materials/RM-1/declarations", json={"allergens": ["casein"]})
    assert decls.status_code == 201
    live = client.get("/batches/OUT-1/trace").json()
    assert live["allergens"] == ["milk"]
    # 并码后新签发标签按当前规范代码冻结
    label2 = client.post("/batches/OUT-1/labels", json={}).json()
    assert label2["conclusion"]["allergens"] == ["milk"]


# ---------------------------------------------------------------- 事件流

def test_merge_records_events_only_for_actual_changes(client):
    material(client, "RM-A", allergens=["casein"])
    material(client, "RM-B", allergens=["gluten"])  # 不受影响
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-A"], ["OUT-1"])
    snap = client.get("/changes/snapshot").json()
    merge(client, "casein", "milk")
    ev = client.get(f"/changes/events?cursor={snap['cursor']}&limit=100").json()
    assert {e["batch_id"] for e in ev["events"]} == {"RM-A", "OUT-1"}
    assert all(e["op"] == "allergen_code_merge" for e in ev["events"])
    op_refs = {e["batch_id"]: e["op_ref"] for e in ev["events"]}
    assert op_refs["RM-A"]["old_code"] == "casein"
    assert op_refs["RM-A"]["target_code"] == "milk"
    # 全局顺序：同次操作按批次号排序
    assert [e["batch_id"] for e in ev["events"]] == sorted(
        e["batch_id"] for e in ev["events"])
    # 仅实际变化：事件 after 为 milk，RM-B 无事件
    out_ev = next(e for e in ev["events"] if e["batch_id"] == "OUT-1")
    assert out_ev["after"]["conclusion"]["allergens"] == ["milk"]
    assert out_ev["before"]["conclusion"]["allergens"] == ["casein"]


def test_failed_merge_records_no_event(client):
    material(client, "RM-1", allergens=["milk"])
    before_max = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    merge(client, "milk", "milk")
    after_max = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    assert before_max == after_max


# ---------------------------------------------------------------- 新登记/更正/预演按当前并码解释

def test_new_registration_and_correction_follow_current_merges(client):
    material(client, "RM-1", allergens=["milk"])
    material(client, "RM-2", allergens=["casein"])
    merge(client, "casein", "milk")
    # 并码后新原料用旧代码登记：实时结论按规范代码解释
    material(client, "RM-3", allergens=["casein"])
    t = client.get("/batches/RM-3/trace").json()
    assert t["allergens"] == ["milk"]
    step = t["allergen_paths"]["milk"][0][0]
    assert step["allergen_code"] == "casein"
    # 声明更正同样按当前并码关系解释
    client.post("/materials/RM-1/declarations", json={"allergens": ["casein"]})
    t1 = client.get("/batches/RM-1/trace").json()
    assert t1["allergens"] == ["milk"]
    assert t1["basis"]["material_declarations"][0]["version"] == 2


def test_declaration_preview_follows_current_merges(client):
    material(client, "RM-1", allergens=["milk", "casein"])
    merge(client, "casein", "milk")
    # 现行声明 [milk, casein] 规范口径只有 milk；预演更正为 [casein, milk]
    # （同集合、不同书写顺序），按当前并码关系解释后结论的过敏原/待确认无差异
    # （追加新版本本身仍体现为依据版本变化，与既有更正语义一致）
    r = client.post("/materials/RM-1/declarations/preview",
                    json={"allergens": ["casein", "milk"]}).json()
    assert r["affected_batches"][0]["diff"]["conclusion"] is None
    # 但用原始代码 [casein] 更正（去掉 milk 同义书写项，版本/依据变化）仍有差异，
    # 差异以规范口径呈现；且声明中记录的原始代码不变（存储层不规范化）
    r2 = client.post("/materials/RM-1/declarations/preview",
                     json={"allergens": ["casein"]}).json()
    assert r2["affected_batches"][0]["batch_id"] == "RM-1"
    assert r2["change"]["allergens"] == ["casein"]


def test_concurrent_merge_invalidates_declaration_preview(client):
    material(client, "RM-1", allergens=["milk"])
    prev = client.post("/materials/RM-1/declarations/preview",
                       json={"allergens": ["soy"]}).json()
    token = prev["preview_id"]
    # 预演期间发生并码（即使与 RM-1 无关）：解释口径已变，凭据失效
    material(client, "RM-2", allergens=["nut", "tree_nut"])
    merge(client, "tree_nut", "nut")
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 409


# ---------------------------------------------------------------- 兼容性

def test_existing_trace_fields_and_paths_remain_compatible(client):
    material(client, "RM-1", allergens=["milk", "peanut"], pending=True)
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    t = client.get("/batches/OUT-1/trace").json()
    # 既有字段结构不变
    assert set(t) >= {"batch_id", "kind", "pending", "allergens",
                      "allergen_paths", "pending_paths", "basis", "release"}
    assert t["allergens"] == ["milk", "peanut"]
    step = t["allergen_paths"]["milk"][0][0]
    assert step["batch_id"] == "RM-1" and step["via"] == "declaration"
    # 无并码时步骤与原结构一致（不额外带代码字段）
    assert "allergen_code" not in step


# ---------------------------------------------------------------- 并码撤销

def test_revoke_unknown_record_404_without_event(client):
    material(client, "RM-1", allergens=["milk"])
    before = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    r = revoke(client, 9999)
    assert r.status_code == 404
    after = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    assert before == after  # 不留事件
    assert client.get("/allergen-merges").json()["merges"] == []


def test_revoke_requires_nonblank_reason(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    r = client.post(f"/allergen-merges/{mid}/revoke", json={"reason": "   "})
    assert r.status_code == 422
    # 缺字段同样 422
    assert client.post(f"/allergen-merges/{mid}/revoke", json={}).status_code == 422
    # 关系仍生效
    assert client.get("/allergen-merges?status=active").json()["merges"][0]["revoked"] is False


def test_revoke_twice_rejected(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    assert revoke(client, mid, reason="第一次撤销").status_code == 200
    r = revoke(client, mid, reason="再次撤销")
    assert r.status_code == 409
    body = client.get("/allergen-merges?status=revoked").json()["merges"][0]
    assert body["revoke_reason"] == "第一次撤销"  # 原因不被覆盖


def test_chain_resolves_only_effective_relations(client):
    # A(casein) -> B(dairy) -> C(milk)
    material(client, "RM-1", allergens=["casein", "dairy", "milk"])
    ab = merge(client, "casein", "dairy").json()["merge_id"]
    bc = merge(client, "dairy", "milk").json()["merge_id"]
    assert client.get("/allergen-codes/casein").json()["canonical_code"] == "milk"
    # 撤销 B->C 后：A 归 B（casein->dairy），B 归自身
    assert revoke(client, bc).status_code == 200
    info = client.get("/allergen-codes/casein").json()
    assert info["canonical_code"] == "dairy"
    assert info["is_alias"] is True
    assert [(h["old_code"], h["target_code"]) for h in info["history"]] == [
        ("casein", "dairy")]  # 已撤销边不出现在生效链沿革
    assert client.get("/allergen-codes/dairy").json()["canonical_code"] == "dairy"
    # 再撤销 A->B 后：B 仍归 C？——B->C 已撤销，故 B 归自身
    assert revoke(client, ab).status_code == 200
    assert client.get("/allergen-codes/casein").json()["canonical_code"] == "casein"
    assert client.get("/allergen-codes/casein").json()["history"] == []
    assert client.get("/allergen-codes/dairy").json()["canonical_code"] == "dairy"


def test_revoke_middle_edge_keeps_upper_chain(client):
    # A->B->C，撤销 A->B 后 B 仍归 C（B->C 继续生效）
    material(client, "RM-1", allergens=["casein", "dairy", "milk"])
    ab = merge(client, "casein", "dairy").json()["merge_id"]
    merge(client, "dairy", "milk")
    revoke(client, ab)
    assert client.get("/allergen-codes/casein").json()["canonical_code"] == "casein"
    info_b = client.get("/allergen-codes/dairy").json()
    assert info_b["canonical_code"] == "milk"
    assert [(h["old_code"], h["target_code"]) for h in info_b["history"]] == [
        ("dairy", "milk")]


def test_merge_list_keeps_original_and_revocation_info(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk", reason="登记原因").json()["merge_id"]
    rv = revoke(client, mid, reason="质控复核：登记有误").json()
    assert rv["revoked"] is True
    assert rv["revoke_reason"] == "质控复核：登记有误"
    assert rv["revoked_at"]
    assert rv["reason"] == "登记原因"          # 原登记原因保留
    assert rv["old_code"] == "casein" and rv["target_code"] == "milk"
    # 列表可查原登记及撤销原因、时间
    entry = client.get("/allergen-merges").json()["merges"][0]
    assert entry["merge_id"] == mid
    assert entry["revoked"] is True
    assert entry["revoke_reason"] == "质控复核：登记有误" and entry["revoked_at"]
    # 过滤
    assert client.get("/allergen-merges?status=revoked").json()["merges"][0]["merge_id"] == mid
    assert client.get("/allergen-merges?status=active").json()["merges"] == []
    assert client.get("/allergen-merges?status=bad").status_code == 400
    # 生效记录不带撤销键
    mid2 = merge(client, "casein", "milk", reason="重新登记").json()["merge_id"]
    active = client.get("/allergen-merges?status=active").json()["merges"][0]
    assert active["merge_id"] == mid2 and "revoke_reason" not in active
    assert "revoked_at" not in active


def test_remerge_after_revoke_allows_new_nonalias_target(client):
    material(client, "RM-1", allergens=["casein", "milk", "gluten"])
    old = merge(client, "casein", "milk").json()["merge_id"]
    revoke(client, old)
    # 撤销后重新并入当前非别名目标（milk 已不是 casein 的目标别名约束对象）
    r = merge(client, "casein", "gluten", reason="纠正：酪蛋白不归奶")
    assert r.status_code == 201
    new_id = r.json()["merge_id"]
    assert new_id != old  # 另起记录，原撤销记录保留
    assert client.get("/allergen-codes/casein").json()["canonical_code"] == "gluten"
    # 原撤销记录仍可查
    revoked = {m["merge_id"]: m for m in
               client.get("/allergen-merges?status=revoked").json()["merges"]}
    assert old in revoked and revoked[old]["revoked"] is True


def test_illegal_remerge_rejected_without_record_or_event(client):
    material(client, "RM-1", allergens=["casein", "dairy", "milk"])
    merge(client, "casein", "milk")
    merge(client, "dairy", "milk")  # dairy 当前是别名
    # 生效关系未撤销直接改指 -> 409，不留新记录
    assert merge(client, "casein", "dairy").status_code == 409
    active = {m["old_code"]: m for m in
              client.get("/allergen-merges?status=active").json()["merges"]}
    revoke(client, active["casein"]["merge_id"])
    before = client.get("/allergen-merges").json()["merges"]
    # 撤销后并入“当前别名”目标 dairy（dairy->milk 仍生效）-> 409
    r2 = merge(client, "casein", "dairy")
    assert r2.status_code == 409 and "别名" in r2.json()["detail"]
    after = client.get("/allergen-merges").json()["merges"]
    assert before == after  # 非法再并码不留记录
    # 撤销后自映射依旧 409
    assert merge(client, "casein", "casein").status_code == 409
    # 撤销后旧代码未在声明中出现的校验仍有效（构造一个未声明代码）
    assert merge(client, "wheat", "milk", reason="x").status_code == 400
    assert client.get("/allergen-merges").json()["merges"] == after


def test_same_direction_retry_idempotent_only_for_effective_relation(client):
    material(client, "RM-1", allergens=["casein", "milk", "gluten"])
    old = merge(client, "casein", "milk", reason="原登记").json()["merge_id"]
    revoke(client, old)
    # 撤销后以相同 old/target 再请求：不是幂等重试，而是新登记
    r = merge(client, "casein", "milk", reason="重新并入")
    assert r.status_code == 201 and r.json()["merge_id"] != old
    # 新关系生效后同向重试才幂等
    again = merge(client, "casein", "milk", reason="再来一次")
    assert again.status_code == 200
    assert again.json()["merge_id"] == r.json()["merge_id"]
    assert again.json()["reason"] == "重新并入"


def test_revoke_recalculates_trace_and_residue_downstream(client):
    material(client, "RM-MILK", allergens=["casein"],
             declared_at="2026-01-02T06:00:00Z")
    material(client, "RM-OAT", allergens=["gluten"],
             declared_at="2026-01-02T06:00:00Z")
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-MILK"], ["OUT-MILK-1"])
    processing(client, "P2", "EQ-1",
               "2026-01-02T10:00:00Z", "2026-01-02T11:00:00Z",
               ["RM-OAT"], ["OUT-OAT-1"])  # 残留
    mid = merge(client, "casein", "milk").json()["merge_id"]
    assert "milk" in client.get("/batches/OUT-OAT-1/trace").json()["allergens"]
    revoke(client, mid)
    # 撤销后规范口径回到原始代码 casein（含设备残留下游）
    t = client.get("/batches/OUT-OAT-1/trace").json()
    assert t["allergens"] == ["casein", "gluten"]
    assert "milk" not in t["allergens"]
    step = t["allergen_paths"]["casein"][0][0]
    assert "allergen_code" not in step  # 已非别名：声明步骤回到原结构


def test_revoke_records_ordered_events_only_for_changes(client):
    material(client, "RM-A", allergens=["casein"])
    material(client, "RM-Z", allergens=["gluten"])  # 无关批次
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-A"], ["OUT-1"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    snap = client.get("/changes/snapshot").json()
    # 撤销前历史事件不可改写：记录撤销时刻点
    pre_events = client.get(
        f"/changes/events?cursor=0&limit=100").json()["events"]
    revoke(client, mid)
    ev = client.get(
        f"/changes/events?cursor={snap['cursor']}&limit=100").json()["events"]
    assert {e["batch_id"] for e in ev} == {"RM-A", "OUT-1"}
    assert all(e["op"] == "allergen_code_merge_revocation" for e in ev)
    assert [e["batch_id"] for e in ev] == sorted(e["batch_id"] for e in ev)
    out = next(e for e in ev if e["batch_id"] == "OUT-1")
    assert out["before"]["conclusion"]["allergens"] == ["milk"]
    assert out["after"]["conclusion"]["allergens"] == ["casein"]
    assert out["op_ref"] == {"merge_id": mid, "old_code": "casein",
                             "target_code": "milk", "revoke_reason": "质控复核撤销"}
    # 历史事件（撤销前的 allergen_code_merge 事件）仍原样存在
    post_events = client.get(
        f"/changes/events?cursor=0&limit=100").json()["events"]
    assert len(post_events) == len(pre_events) + 2
    assert any(e["op"] == "allergen_code_merge" for e in post_events)


def test_revoke_keeps_raw_declarations_and_label_snapshots(client):
    material(client, "RM-1", allergens=["casein"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    label = client.post("/batches/OUT-1/labels", json={}).json()
    revoke(client, mid)
    # 原料原始声明代码不变
    assert client.get("/materials/RM-1").json()["allergens"] == ["casein"]
    # 已签发标签快照保持撤销前结论（milk），新实时结论回到 casein
    frozen = client.get(f"/labels/{label['label_id']}").json()
    assert frozen["conclusion"]["allergens"] == ["milk"]
    assert client.get("/batches/OUT-1/trace").json()["allergens"] == ["casein"]


def test_revoke_invalidates_outstanding_preview(client):
    material(client, "RM-1", allergens=["milk"])
    prev = client.post("/materials/RM-1/declarations/preview",
                       json={"allergens": ["soy"]}).json()
    token = prev["preview_id"]
    material(client, "RM-2", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    revoke(client, mid)  # 撤销（即使发生在无关链路上）使凭据失效
    r = client.post(f"/previews/{token}/commit")
    assert r.status_code == 409


def test_failed_revoke_leaves_no_trace_change(client):
    material(client, "RM-1", allergens=["casein", "milk"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    before_max = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    # 重复撤销与未知记录均不产事件
    revoke(client, mid)
    assert revoke(client, mid).status_code == 409
    assert revoke(client, mid + 9999).status_code == 404
    after_max = client.get("/changes/events?cursor=0&limit=1").json()["latest"]
    # 仅第一次合法撤销的事件（RM-1）
    assert after_max - before_max == 1


def test_snapshot_reflects_conclusion_after_revoke(client):
    material(client, "RM-1", allergens=["casein"])
    processing(client, "P1", "EQ-1",
               "2026-01-02T08:00:00Z", "2026-01-02T09:00:00Z",
               ["RM-1"], ["OUT-1"])
    mid = merge(client, "casein", "milk").json()["merge_id"]
    revoke(client, mid)
    snap = client.get("/changes/snapshot").json()
    allergens = {b["batch_id"]: b["conclusion"]["allergens"]
                 for b in snap["batches"]}
    assert allergens == {"RM-1": ["casein"], "OUT-1": ["casein"]}
