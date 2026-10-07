"""已受理理赔资格复核（只读）端到端测试。

三份判定（同一订单锁定的政策、时区与日历）：
- accepted_at_review：凭证冻结的受理时判定（不重算）；
- baseline_at_review：事件视图冻结在凭证接收时刻，仅把判定时刻推进到核对时刻；
- current_at_review：纳入截至核对时刻到达的全部扫描与更正后现时重算（归档快照不替代重算）。

归因：
- 时间推移单独把 filable 推成 not_filable => natural_expiry=true，绝归因于更正；
- 后到事件（含 amend/cancel/insert 与普通扫描）在同一核对时刻上的效应单列，
  列出纳入重算的事件编号与更正动作。

拒绝：核对时刻缺失/无偏移/不可解析 422 INVALID_REQUEST；早于凭证接收时刻
422 REVIEW_TIME_BEFORE_RECEIVED；晚于服务当前时刻 422 REVIEW_TIME_IN_FUTURE；
凭证不存在 404 VOUCHER_NOT_FOUND。全程只读。
"""
from tests.conftest import make_order


def _accept_delay(client, oid="O1", key="K1",
                  received_at="2025-02-20T12:00:00+08:00"):
    r = client.post(f"/api/v1/orders/{oid}/claims",
                    json={"claim_type": "delay", "idempotency_key": key,
                          "received_at": received_at})
    assert r.status_code == 201, r.text
    return r.json()


def _review(client, claim_no, as_of):
    return client.get(f"/api/v1/claims/{claim_no}/review", params={"as_of": as_of})


# ------------------------------------------------------------ 三份判定的基本结构

def test_three_views_all_filable_before_deadline(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    v = _accept_delay(client)

    r = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["claim_no"] == v["claim_no"]
    assert body["order_id"] == "O1"
    assert body["claim_type"] == "delay"
    assert body["policy_version"] == "v2"
    assert body["timezone"] == "Asia/Shanghai"
    assert body["calendar_code"] == "CN"
    assert body["read_only"] is True
    assert body["review_at_utc"].startswith("2025-02-24T04:00:00")

    acc, base, cur = (body["accepted_at_review"], body["baseline_at_review"],
                      body["current_at_review"])
    # 受理时判定：冻结快照，不重算
    assert acc["recomputed"] is False
    assert acc["source"] == "voucher_frozen_snapshot"
    assert acc["status"] == "filable"
    assert acc["decision_code"] == "FILABLE"
    assert acc["claim"] == v["claim"]
    assert acc["determination"] == v["determination"]
    assert acc["deadline_local"].startswith("2025-02-25T23:59:59")
    # 基线与现时：重算（即使没有任何后到事件）
    for view in (base, cur):
        assert view["recomputed"] is True
        assert view["status"] == "filable"
        assert view["decision_code"] == "FILABLE"
        assert view["claim"]["claim_type"] == "delay"
        assert view["determination"]["claims"][0]["claim_type"] == "delay"
        assert view["deadline_local"] == acc["deadline_local"]

    tp, la = body["time_passage"], body["late_arrivals"]
    assert tp["status_changed"] is False and tp["deadline_changed"] is False
    assert tp["natural_expiry"] is False
    assert tp["attributed_to_correction"] is False
    assert la["driven_by_late_events"] is False
    assert la["included_late_event_ids"] == []
    assert body["late_events"] == []


def test_frozen_accepted_view_never_recomputed(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    v = _accept_delay(client)
    body = _review(client, v["claim_no"], "2025-02-26T08:00:00+08:00").json()
    # 即便基线/现时已过期，受理冻结视图仍是受理当时的 filable 与原截止
    acc = body["accepted_at_review"]
    assert acc["status"] == "filable"
    assert acc["claim"] == v["claim"]
    assert acc["determination"] == v["determination"]


# ------------------------------------------------------------ 仅时间推移：自然过期

def test_time_passage_natural_expiry_not_attributed_to_correction(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    v = _accept_delay(client)  # 截止 2/25 23:59:59

    body = _review(client, v["claim_no"], "2025-02-26T08:00:00+08:00").json()

    base, cur = body["baseline_at_review"], body["current_at_review"]
    assert base["status"] == "not_filable"
    assert cur["status"] == "not_filable"

    tp = body["time_passage"]
    assert tp["status_before"] == "filable"
    assert tp["status_after"] == "not_filable"
    assert tp["status_changed"] is True
    assert tp["deadline_changed"] is False
    assert tp["natural_expiry"] is True
    assert tp["attributed_to_correction"] is False
    assert "自然过期" in tp["attribution"]

    la = body["late_arrivals"]
    assert la["driven_by_late_events"] is False
    assert la["baseline_already_expired"] is True
    assert la["attributed_to_correction"] is False
    assert "自然过期不归因于任何更正" in la["attribution"]
    assert la["included_late_event_ids"] == []
    assert body["late_events"] == []


# ------------------------------------------------------------ 后到事件：amend

def test_late_amend_changes_current_only(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    v = _accept_delay(client)

    # 受理后到达的更正：签收修正到承诺时限前（未归档，并入第 1 代）
    r = client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-21T08:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1",
        "note": "网点补传准确签收时间"})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1

    body = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00").json()

    # 基线只推进时间、不收更正：仍 filable；现时纳入更正：not_applicable
    assert body["baseline_at_review"]["status"] == "filable"
    assert body["baseline_at_review"]["event_view"]["event_count"] == 1
    cur = body["current_at_review"]
    assert cur["status"] == "not_applicable"
    assert cur["event_view"]["event_count"] == 2

    tp = body["time_passage"]
    assert tp["status_changed"] is False and tp["deadline_changed"] is False
    assert tp["natural_expiry"] is False

    la = body["late_arrivals"]
    assert la["status_before"] == "filable"
    assert la["status_after"] == "not_applicable"
    assert la["status_changed"] is True
    assert la["driven_by_late_events"] is True
    assert la["attributed_to_correction"] is True
    assert la["included_late_event_ids"] == ["c1"]
    assert la["included_late_correction_ids"] == ["c1"]
    assert la["late_events_count"] == 1
    assert la["late_corrections_count"] == 1
    assert la["late_scans_count"] == 0
    assert la["baseline_already_expired"] is False

    ev = body["late_events"][0]
    assert ev["event_id"] == "c1"
    assert ev["is_correction"] is True
    assert ev["correction_action"] == "amend"
    assert ev["action"] == "amend"
    assert ev["target_event_id"] == "s1"
    assert "amend" in ev["action_label"]
    # 基线判定依据明确不纳入受理后事件；现时明确已纳入
    assert any("受理后到达的事件一律不纳入" in b
               for b in body["baseline_at_review"]["determination"]["basis"])
    assert any("受理复核·现时判定" in b
               for b in body["current_at_review"]["determination"]["basis"])


def test_late_amend_after_archived_opens_new_generation_and_snapshot_not_used(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    v = _accept_delay(client)

    # 受理后归档第 1 代，再由迟到更正开启第 2 代
    arch = client.post("/api/v1/orders/O1/determination/archive",
                       params={"as_of": "2025-02-21T12:00:00+08:00"})
    assert arch.status_code == 200
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-22T08:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1"})

    body = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00").json()

    # 现时按第 2 代重算，不采用第 1 代归档快照（快照里 delay=filable）
    cur = body["current_at_review"]
    assert cur["status"] == "not_applicable"
    assert cur["event_view"]["generation_no"] == 2
    assert cur["recomputed"] is True
    # 基线事件视图只含接收时刻前事件，落在第 1 代；重算而非读快照
    assert body["baseline_at_review"]["status"] == "filable"
    assert body["baseline_at_review"]["event_view"]["generation_no"] == 1
    assert body["baseline_at_review"]["recomputed"] is True
    # 受理冻结视图仍是第 1 代凭证
    assert body["accepted_at_review"]["event_view"]["generation_no"] == 1

    la = body["late_arrivals"]
    assert la["driven_by_late_events"] is True
    assert la["included_late_event_ids"] == ["c1"]
    assert body["late_events"][0]["generation_no"] == 2


def test_accepted_archived_recheck_flag_preserved_in_review(client):
    # 先归档（filable 快照），再提交申请并受理：凭证带 archived_generation_rechecked
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-12T10:00:00+08:00"})
    r = client.post("/api/v1/orders/O1/claims",
                    json={"claim_type": "delay", "idempotency_key": "K1",
                          "received_at": "2025-02-20T12:00:00+08:00"})
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["archived_generation_rechecked"] is True

    body = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00").json()
    assert body["accepted_archived_generation_rechecked"] is True


# ------------------------------------------------------------ 后到事件：cancel / insert / 普通扫描

def test_late_cancel_returns_loss_to_pending(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "l1", "event_type": "lost_confirmed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    r = client.post("/api/v1/orders/O1/claims",
                    json={"claim_type": "loss", "idempotency_key": "KL",
                          "received_at": "2025-02-20T12:00:00+08:00"})
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["decision_code"] == "FILABLE"

    client.post("/api/v1/orders/O1/events", json={
        "event_id": "x1", "event_type": "lost_confirmed",
        "event_time": "2025-02-21T09:00:00+08:00",
        "arrived_at": "2025-02-21T10:00:00+08:00",
        "correction_action": "cancel", "target_event_id": "l1",
        "note": "丢件确认被推翻"})

    body = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00").json()
    assert body["baseline_at_review"]["status"] == "filable"
    assert body["current_at_review"]["status"] == "pending"
    assert body["current_at_review"]["deadline_utc"] is None
    la = body["late_arrivals"]
    assert la["status_changed"] is True
    assert la["deadline_changed"] is True
    assert la["driven_by_late_events"] is True
    assert la["attributed_to_correction"] is True
    item = body["late_events"][0]
    assert item["event_id"] == "x1"
    assert item["action"] == "cancel"
    assert "撤销" in item["action_label"]
    assert item["target_event_id"] == "l1"


def test_late_insert_moves_deadline_but_status_stays_filable(client):
    # 破损：2/11 触发，3 个工作日截止 2/14；2/12 受理（filable）
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "g1", "event_type": "damage_reported",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    r = client.post("/api/v1/orders/O1/claims",
                    json={"claim_type": "damage", "idempotency_key": "KD",
                          "received_at": "2025-02-12T10:00:00+08:00"})
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["deadline_local"].startswith("2025-02-14T23:59:59")

    # 受理后补录更早的破损申报（2/10）：新截止 2/13；核对时刻 2/13 中午仍 filable
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "i1", "event_type": "damage_reported",
        "event_time": "2025-02-10T09:00:00+08:00",
        "arrived_at": "2025-02-13T08:00:00+08:00",
        "correction_action": "insert", "note": "补录现场申报"})

    body = _review(client, v["claim_no"], "2025-02-13T12:00:00+08:00").json()
    base, cur = body["baseline_at_review"], body["current_at_review"]
    assert base["status"] == "filable"
    assert base["deadline_local"].startswith("2025-02-14T23:59:59")
    assert cur["status"] == "filable"
    assert cur["deadline_local"].startswith("2025-02-13T23:59:59")

    tp = body["time_passage"]
    assert tp["status_changed"] is False and tp["deadline_changed"] is False
    la = body["late_arrivals"]
    assert la["status_changed"] is False
    assert la["deadline_changed"] is True          # 后到 insert 提前了截止时刻
    assert la["driven_by_late_events"] is True
    assert la["attributed_to_correction"] is True
    assert la["included_late_event_ids"] == ["i1"]
    assert body["late_events"][0]["action"] == "insert"
    assert "补录" in body["late_events"][0]["action_label"]


def test_late_plain_scan_changes_status_but_not_marked_as_correction(client):
    # 承诺时限已过未签收 => 2/12 受理 delay（anchor=承诺时限）；
    # 受理后到达一条普通签收扫描（非更正，签收时间准时）=> 现时 not_applicable
    make_order(client)
    v = _accept_delay(client, received_at="2025-02-12T10:00:00+08:00")

    ing = client.post("/api/v1/orders/O1/events", json={
        "event_id": "s9", "event_type": "signed",
        "event_time": "2025-02-09T18:00:00+08:00",
        "arrived_at": "2025-02-13T08:00:00+08:00"})
    assert ing.status_code == 200, ing.text
    assert ing.json()["accepted"] == 1

    body = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00").json()
    assert body["baseline_at_review"]["status"] == "filable"
    assert body["current_at_review"]["status"] == "not_applicable"
    la = body["late_arrivals"]
    assert la["driven_by_late_events"] is True
    assert la["included_late_event_ids"] == ["s9"]
    assert la["included_late_correction_ids"] == []
    assert la["late_scans_count"] == 1 and la["late_corrections_count"] == 0
    # 由后到事件造成，但不是更正：不得记为更正归因
    assert la["attributed_to_correction"] is False
    item = body["late_events"][0]
    assert item["is_correction"] is False
    assert item["action"] == "scan"
    assert item["correction_action"] is None
    assert "普通扫描" in item["action_label"]


def test_event_arriving_after_review_time_is_excluded(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    v = _accept_delay(client)
    # 核对时刻之后才到达的更正，现时重算不得纳入
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "c-later", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-26T08:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1"})

    body = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00").json()
    assert body["current_at_review"]["status"] == "filable"
    assert body["current_at_review"]["event_view"]["event_count"] == 1
    assert body["late_arrivals"]["included_late_event_ids"] == []
    assert body["late_events"] == []
    assert body["late_arrivals"]["driven_by_late_events"] is False


def test_baseline_natural_expiry_plus_late_insert_deadline_change(client):
    # 基线已自然过期（截止 2/14），后到 insert 又把截止提前到 2/11：
    # 状态同为 not_filable，但截止时刻被后到更正改变；自然过期仍不归因更正
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "g1", "event_type": "damage_reported",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    r = client.post("/api/v1/orders/O1/claims",
                    json={"claim_type": "damage", "idempotency_key": "KD",
                          "received_at": "2025-02-12T10:00:00+08:00"})
    assert r.status_code == 201, r.text
    v = r.json()
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "i1", "event_type": "damage_reported",
        "event_time": "2025-02-06T09:00:00+08:00",
        "arrived_at": "2025-02-21T08:00:00+08:00",
        "correction_action": "insert"})

    body = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00").json()
    assert body["baseline_at_review"]["status"] == "not_filable"
    assert body["current_at_review"]["status"] == "not_filable"
    # 2/6（周四）起 3 个工作日：2/7、2/8（春节调休补班，虽为周六也计入）、2/10
    assert body["current_at_review"]["deadline_local"].startswith("2025-02-10")
    tp = body["time_passage"]
    assert tp["natural_expiry"] is True
    assert tp["attributed_to_correction"] is False
    la = body["late_arrivals"]
    assert la["baseline_already_expired"] is True
    assert la["deadline_changed"] is True
    assert la["driven_by_late_events"] is True
    assert "自然过期部分仍不归因于更正" in la["attribution"]


# ------------------------------------------------------------ 锁定政策与时区

def test_review_uses_order_locked_policy_and_timezone(client):
    # v1 政策订单（2024）：延迟 7 个工作日
    make_order(client, "OLD", created_at="2024-06-03T09:00:00+08:00",
               promise_by="2024-06-10T18:00:00+08:00")
    client.post("/api/v1/orders/OLD/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2024-06-11T10:00:00+08:00",
        "arrived_at": "2024-06-11T10:05:00+08:00"})
    r = client.post("/api/v1/orders/OLD/claims",
                    json={"claim_type": "delay", "idempotency_key": "K1",
                          "received_at": "2024-06-12T10:00:00+08:00"})
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["policy_version"] == "v1"

    body = _review(client, v["claim_no"], "2024-06-18T10:00:00+08:00").json()
    assert body["policy_version"] == "v1"
    assert body["timezone"] == "Asia/Shanghai"
    for view in (body["accepted_at_review"], body["baseline_at_review"],
                 body["current_at_review"]):
        assert view["determination"]["policy_version"] == "v1"
        if view["deadline_local"]:
            assert view["deadline_local"].endswith("+08:00")


# ------------------------------------------------------------ 边界与拒绝

def test_review_at_exactly_received_time_allowed(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    v = _accept_delay(client)
    r = _review(client, v["claim_no"], "2025-02-20T12:00:00+08:00")
    assert r.status_code == 200, r.text


def test_missing_as_of_rejected(client):
    make_order(client)
    v = _accept_delay(client)
    r = client.get(f"/api/v1/claims/{v['claim_no']}/review")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    assert err["details"]["field"] == "as_of"


def test_naive_as_of_rejected(client):
    make_order(client)
    v = _accept_delay(client)
    r = _review(client, v["claim_no"], "2025-02-24T12:00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_unparseable_as_of_rejected(client):
    make_order(client)
    v = _accept_delay(client)
    r = _review(client, v["claim_no"], "not-a-time")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_review_before_received_time_rejected(client):
    make_order(client)
    v = _accept_delay(client)
    r = _review(client, v["claim_no"], "2025-02-19T12:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "REVIEW_TIME_BEFORE_RECEIVED"
    assert err["details"]["review_at_utc"].startswith("2025-02-19")
    assert err["details"]["received_at_utc"].startswith("2025-02-20")


def test_review_in_future_rejected(client):
    make_order(client)
    v = _accept_delay(client)
    r = _review(client, v["claim_no"], "2030-01-01T00:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "REVIEW_TIME_IN_FUTURE"
    assert "served_at_utc" in err["details"]


def test_review_unknown_voucher_404(client):
    r = _review(client, "CLM-NOPE", "2025-02-24T12:00:00+08:00")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "VOUCHER_NOT_FOUND"


# ------------------------------------------------------------ 只读保证

def test_review_is_read_only(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    v = _accept_delay(client)

    events_before = client.get("/api/v1/orders/O1/events").json()
    gens_before = client.get("/api/v1/orders/O1/determinations").json()
    voucher_before = client.get(f"/api/v1/claims/{v['claim_no']}").json()

    r1 = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00")
    r2 = _review(client, v["claim_no"], "2025-02-24T12:00:00+08:00")
    assert r1.status_code == r2.status_code == 200
    j1, j2 = r1.json(), r2.json()
    # served_at_utc 为服务时刻戳，天然逐请求变化；其余内容必须逐字节一致
    assert j1.pop("served_at_utc") != "" and j2.pop("served_at_utc") != ""
    assert j1 == j2

    assert client.get("/api/v1/orders/O1/events").json() == events_before
    assert client.get("/api/v1/orders/O1/determinations").json() == gens_before
    assert client.get(f"/api/v1/claims/{v['claim_no']}").json() == voucher_before
    # 凭证表没有新增
    assert len(client.get("/api/v1/orders/O1/claims").json()["vouchers"]) == 1
