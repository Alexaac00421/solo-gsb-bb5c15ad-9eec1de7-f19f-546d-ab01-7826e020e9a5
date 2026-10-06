"""已受理理赔复核（只读）端到端测试：

- 按凭证编号 + 带偏移核对时刻返回三视图：凭证冻结的受理时判定、
  仅推进时间的基线判定（不纳入受理后到达的事件）、纳入截至核对时刻
  到达的扫描与更正的现时判定；
- 重算使用订单锁定的政策/时区/日历，归档快照不替代重算；
- 时间推移与后到事件分别标注是否改变该类型状态或截止时刻；
- 列出纳入现时重算的后到事件编号及更正动作；自然过期不归因于更正；
- 核对时刻早于接收时刻 / 晚于服务当前时刻 / 缺时区偏移 / 凭证不存在均拒绝；
- 查询只读，不改变凭证、事件、归档或受理规则。
"""
from tests.conftest import make_order

SIGNED_LATE = {
    "event_id": "s1", "event_type": "signed",
    "event_time": "2025-02-11T10:00:00+08:00",
    "arrived_at": "2025-02-11T10:05:00+08:00",
}
RECEIVED = "2025-02-20T12:00:00+08:00"          # 凭证接收时刻（UTC 2/20 04:00）


def _accept(client, oid="O1", claim_type="delay", key="K1", received_at=RECEIVED):
    r = client.post(f"/api/v1/orders/{oid}/claims", json={
        "claim_type": claim_type, "idempotency_key": key,
        "received_at": received_at})
    assert r.status_code == 201, r.text
    return r.json()["claim_no"]


def _review(client, claim_no, as_of):
    return client.get(f"/api/v1/claims/{claim_no}/review", params={"as_of": as_of})


def _amend_signed(client, new_time, arrived, oid="O1", event_id="c1", target="s1"):
    return client.post(f"/api/v1/orders/{oid}/events", json={
        "event_id": event_id, "event_type": "signed",
        "event_time": new_time, "arrived_at": arrived,
        "correction_action": "amend", "target_event_id": target})


# ------------------------------------------------------------ 三视图与归因

def test_review_three_views_when_nothing_changes(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)

    r = _review(client, claim_no, "2025-02-24T12:00:00+08:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claim_no"] == claim_no
    assert body["order_id"] == "O1"
    assert body["claim_type"] == "delay" and body["claim_name"] == "延迟送达"
    assert body["decision_code"] == "FILABLE"
    assert body["as_of_utc"] == "2025-02-24T04:00:00+00:00"
    assert body["as_of_local"].startswith("2025-02-24T12:00:00+08:00")
    assert body["received_at_utc"] == "2025-02-20T04:00:00+00:00"
    assert body["policy_version"] == "v2"
    assert body["timezone"] == "Asia/Shanghai"
    assert body["calendar_code"] == "CN"
    assert body["read_only"] is True

    det = body["determinations"]
    # 受理时判定：凭证冻结，不重算
    assert det["accepted"]["source"] == "voucher_frozen"
    assert det["accepted"]["evaluated_at_utc"] == "2025-02-20T04:00:00+00:00"
    assert det["accepted"]["events_cutoff_utc"] == "2025-02-20T04:00:00+00:00"
    # 基线：推进到核对时刻，但事件仍截止到接收时刻
    assert det["baseline"]["source"] == "recomputed"
    assert det["baseline"]["evaluated_at_utc"] == "2025-02-24T04:00:00+00:00"
    assert det["baseline"]["events_cutoff_utc"] == "2025-02-20T04:00:00+00:00"
    # 现时：事件截止到核对时刻
    assert det["current"]["events_cutoff_utc"] == "2025-02-24T04:00:00+00:00"
    for view in det.values():
        assert view["archived_snapshot_used"] is False
        assert view["status"] == "filable"
        assert view["deadline_local"] == "2025-02-25T23:59:59+08:00"
        assert view["deadline_utc"] == "2025-02-25T15:59:59+00:00"

    tp = body["time_passage"]
    assert tp["changed"] is False and tp["status_changed"] is False
    assert tp["deadline_changed"] is False
    le = body["late_events"]
    assert le["changed"] is False
    assert le["event_count"] == 0 and le["correction_count"] == 0
    assert le["events"] == []
    assert "无新到事件" in le["note"]


def test_review_natural_expiry_attributed_to_time_passage(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)

    # 核对时刻 2/26：截止 2/25 23:59:59 已过 -> 自然过期
    body = _review(client, claim_no, "2025-02-26T08:00:00+08:00").json()
    det = body["determinations"]
    assert det["accepted"]["status"] == "filable"
    assert det["baseline"]["status"] == "not_filable"
    assert det["current"]["status"] == "not_filable"

    tp = body["time_passage"]
    assert tp["status_changed"] is True
    assert tp["status_before"] == "filable" and tp["status_after"] == "not_filable"
    assert tp["deadline_changed"] is False      # 截止时刻不变，只是时间到了
    assert "自然过期" in tp["note"]
    assert body["late_events"]["changed"] is False


def test_review_late_correction_changes_current_only(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)
    # 受理后到达的更正：签收修正为 2/9（未迟到）
    _amend_signed(client, "2025-02-09T09:00:00+08:00", "2025-02-21T08:00:00+08:00")

    body = _review(client, claim_no, "2025-02-22T12:00:00+08:00").json()
    det = body["determinations"]
    assert det["accepted"]["status"] == "filable"
    assert det["baseline"]["status"] == "filable"      # 不纳入受理后事件
    assert det["current"]["status"] == "not_applicable"

    tp = body["time_passage"]
    assert tp["changed"] is False                      # 时间推移无影响
    le = body["late_events"]
    assert le["status_changed"] is True
    assert le["status_before"] == "filable"
    assert le["status_after"] == "not_applicable"
    assert le["event_count"] == 1 and le["correction_count"] == 1
    ev = le["events"][0]
    assert ev["event_id"] == "c1"
    assert ev["is_correction"] is True
    assert ev["correction_action"] == "amend"
    assert ev["target_event_id"] == "s1"
    assert ev["arrived_at_utc"] == "2025-02-21T00:00:00+00:00"


def test_review_natural_expiry_not_attributed_to_correction(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)
    # 受理后到达的更正：签收仍迟到（2/11 12:00），不改变状态与截止
    _amend_signed(client, "2025-02-11T12:00:00+08:00", "2025-02-21T08:00:00+08:00")

    # 核对时刻已过截止：基线与现时同为 not_filable（自然过期）
    body = _review(client, claim_no, "2025-02-26T08:00:00+08:00").json()
    det = body["determinations"]
    assert det["baseline"]["status"] == "not_filable"
    assert det["current"]["status"] == "not_filable"
    le = body["late_events"]
    assert le["changed"] is False                      # 更正未改变结果
    assert le["event_count"] == 1 and le["correction_count"] == 1
    # 自然过期不能归因于更正
    assert "自然过期" in le["note"]
    assert "不能归因于更正" in le["note"]
    assert "自然过期" in body["time_passage"]["note"]


def test_review_late_event_changes_deadline_only(client):
    # 受理时（2/12）无签收扫描：承诺 2/10 已过 -> filable，锚点承诺时限，截止 2/24
    make_order(client)
    claim_no = _accept(client, received_at="2025-02-12T10:00:00+08:00")
    # 受理后到达的迟到签收扫描（非更正事件）：锚点变为实际签收 2/11，截止 2/25
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-13T09:00:00+08:00"})

    body = _review(client, claim_no, "2025-02-14T12:00:00+08:00").json()
    det = body["determinations"]
    assert det["accepted"]["deadline_local"].startswith("2025-02-24T23:59:59")
    assert det["baseline"]["deadline_local"].startswith("2025-02-24T23:59:59")
    assert det["current"]["deadline_local"].startswith("2025-02-25T23:59:59")
    assert det["current"]["status"] == "filable"

    assert body["time_passage"]["changed"] is False
    le = body["late_events"]
    assert le["status_changed"] is False
    assert le["deadline_changed"] is True              # 仅截止时刻被后到事件改变
    assert le["changed"] is True
    ev = le["events"][0]
    assert ev["event_id"] == "s1"
    assert ev["is_correction"] is False
    assert ev["correction_action"] is None


def test_review_as_of_filters_late_events(client):
    make_order(client)
    claim_no = _accept(client, received_at="2025-02-12T10:00:00+08:00")
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-13T09:00:00+08:00"})

    # 核对时刻 2/12 12:00 早于签收扫描到达（2/13）：现时判定同样看不到它
    body = _review(client, claim_no, "2025-02-12T12:00:00+08:00").json()
    det = body["determinations"]
    assert det["current"]["deadline_local"].startswith("2025-02-24T23:59:59")
    le = body["late_events"]
    assert le["events"] == [] and le["event_count"] == 0
    assert le["changed"] is False


def test_review_ignores_archived_snapshot(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)
    # 归档第 1 代（快照为 filable），再由迟到更正开启第 2 代
    client.post("/api/v1/orders/O1/determination/archive",
                params={"as_of": "2025-02-20T12:00:00+08:00"})
    _amend_signed(client, "2025-02-09T09:00:00+08:00", "2025-02-21T08:00:00+08:00")

    body = _review(client, claim_no, "2025-02-22T12:00:00+08:00").json()
    det = body["determinations"]
    # 归档快照（filable）不得替代重算：现时判定必须为 not_applicable
    assert det["current"]["status"] == "not_applicable"
    assert det["current"]["archived_snapshot_used"] is False
    assert det["baseline"]["archived_snapshot_used"] is False
    assert det["baseline"]["status"] == "filable"
    assert body["late_events"]["status_changed"] is True


def test_review_late_cancel_correction_listed(client):
    # 破损申报 2/11 11:00 到达，2/12 受理（v2：3 个工作日，截止 2/14）
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "g1", "event_type": "damage_reported",
        "event_time": "2025-02-11T11:00:00+08:00",
        "arrived_at": "2025-02-11T11:10:00+08:00"})
    claim_no = _accept(client, claim_type="damage",
                       received_at="2025-02-12T10:00:00+08:00")
    # 受理后到达的更正：撤销破损申报
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "c1", "event_type": "damage_reported",
        "event_time": "2025-02-11T11:00:00+08:00",
        "arrived_at": "2025-02-13T09:00:00+08:00",
        "correction_action": "cancel", "target_event_id": "g1"})

    body = _review(client, claim_no, "2025-02-13T12:00:00+08:00").json()
    det = body["determinations"]
    assert det["accepted"]["status"] == "filable"
    assert det["accepted"]["deadline_local"].startswith("2025-02-14T23:59:59")
    assert det["baseline"]["status"] == "filable"
    assert det["current"]["status"] == "pending"       # 触发事件被撤销
    le = body["late_events"]
    assert le["status_changed"] is True
    assert le["status_before"] == "filable" and le["status_after"] == "pending"
    ev = le["events"][0]
    assert ev["event_id"] == "c1"
    assert ev["correction_action"] == "cancel"
    assert ev["target_event_id"] == "g1"
    assert ev["event_name"] == "破损申报"


def test_review_at_exact_received_time(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)
    # 核对时刻 == 凭证接收时刻：允许（不早于）
    r = _review(client, claim_no, RECEIVED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["determinations"]["baseline"]["status"] == "filable"
    assert body["determinations"]["accepted"]["status"] == "filable"
    assert body["time_passage"]["changed"] is False
    assert body["late_events"]["events"] == []


# ------------------------------------------------------------ 明确拒绝

def test_review_as_of_before_received_rejected(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)
    r = _review(client, claim_no, "2025-02-20T11:00:00+08:00")  # 早于接收 12:00
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "REVIEW_AS_OF_BEFORE_RECEIVED"
    assert err["details"]["received_at_utc"] == "2025-02-20T04:00:00+00:00"
    assert err["details"]["claim_no"] == claim_no


def test_review_future_as_of_rejected(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)
    r = _review(client, claim_no, "2999-01-01T00:00:00+08:00")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "REVIEW_AS_OF_IN_FUTURE"
    assert err["details"]["server_now_utc"]


def test_review_naive_or_missing_as_of_rejected(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)
    # 缺少时区偏移
    r = _review(client, claim_no, "2025-02-21T12:00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
    # 非法格式
    r = _review(client, claim_no, "not-a-time")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
    # 缺失 as_of
    r = client.get(f"/api/v1/claims/{claim_no}/review")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_review_unknown_voucher_404(client):
    r = _review(client, "CLM-NOPE", "2025-02-21T12:00:00+08:00")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "VOUCHER_NOT_FOUND"


# ------------------------------------------------------------ 只读语义

def test_review_is_read_only(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json=SIGNED_LATE)
    claim_no = _accept(client)
    _amend_signed(client, "2025-02-09T09:00:00+08:00", "2025-02-21T08:00:00+08:00")

    voucher_before = client.get(f"/api/v1/claims/{claim_no}").json()
    history_before = client.get("/api/v1/orders/O1/determinations").json()
    events_before = client.get("/api/v1/orders/O1/events").json()

    r = _review(client, claim_no, "2025-02-22T12:00:00+08:00")
    assert r.status_code == 200 and r.json()["read_only"] is True
    # 再次复核结果一致（纯查询，无副作用）
    assert _review(client, claim_no, "2025-02-22T12:00:00+08:00").json() == r.json()

    # 凭证、判定历史（归档/代）、事件流均未被改变
    assert client.get(f"/api/v1/claims/{claim_no}").json() == voucher_before
    assert client.get("/api/v1/orders/O1/determinations").json() == history_before
    assert client.get("/api/v1/orders/O1/events").json() == events_before
