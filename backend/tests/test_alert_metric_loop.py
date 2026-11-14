"""预警指标口径闭环的验收测试：状态、事务、版本与分页。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store


@pytest.fixture(autouse=True)
def _reset_store():
    store.reset()
    yield
    store.reset()


@pytest.fixture()
def client():
    return TestClient(app)


def _action(client, entry_id, action, **values):
    payload = {"action": action, **values}
    return client.post(f"/api/alertconfig/{entry_id}/actions", json={"values": payload}).json()


def _metric_action(client, entry_id, action, **values):
    payload = {"action": action, **values}
    return client.post(f"/api/metric/{entry_id}/actions", json={"values": payload}).json()


def _trigger(client, entry_id, value, cycle, occurred_at=None):
    values = {"取值": value, "周期": cycle}
    if occurred_at:
        values["发生时间"] = occurred_at
    return client.post(f"/api/alertconfig/{entry_id}/trigger", json={"values": values}).json()


def _rule(client, entry_id):
    return client.get(f"/api/alertconfig/{entry_id}").json()


def test_closed_rule_keeps_streak_and_rejects_trigger(client):
    rule = _rule(client, 1)
    assert rule["status"] == "已关闭"
    assert rule["连续周期数"] == 5
    result = _trigger(client, 1, "99.92", "2026-10-12")
    assert result["ok"] is False
    assert "已关闭" in result["message"]
    after = _rule(client, 1)
    assert after["连续周期数"] == 5
    assert after["关闭前连续周期"] == 5
    assert len(client.get("/api/alertconfig/1/notifications").json()) == 2
    # 重复关闭也是被拒绝的，历史值原样保留。
    assert _action(client, 1, "关闭规则")["ok"] is False
    kept = _rule(client, 1)
    assert kept["预警编号"] == "ALER-2026-071"
    assert kept["比较运算符"] == "greater_than"
    assert kept["静默时段"] == "00:00-06:00"
    assert kept["阈值数值"] == "99.5"


def test_threshold_cannot_change_after_activation(client):
    assert _action(client, 2, "发起修订")["ok"] is True
    rule = _rule(client, 2)
    assert rule["status"] == "草稿"
    updated = client.post(
        "/api/alertconfig/2",
        json={"values": {"阈值数值": "40", "比较运算符": "greater_than", "expected_rule_version": rule["规则版本"]}},
    ).json()
    assert updated["ok"] is True
    # 并发修改：旧版本号提交第二次会被拦下。
    stale = client.post(
        "/api/alertconfig/2",
        json={"values": {"阈值数值": "41", "expected_rule_version": rule["规则版本"]}},
    ).json()
    assert stale["ok"] is False
    current = _rule(client, 2)
    assert current["阈值数值"] == "40"
    # 提交评审后阈值就不能再改，但可以撤回草稿。
    assert _action(client, 2, "提交规则评审")["ok"] is True
    blocked = client.post("/api/alertconfig/2", json={"values": {"阈值数值": "45"}}).json()
    assert blocked["ok"] is False
    assert _action(client, 2, "撤回评审")["ok"] is True
    assert _rule(client, 2)["status"] == "草稿"


def test_activation_requires_published_version_and_bound_threshold(client):
    created = client.post(
        "/api/alertconfig",
        json={"values": {"预警编号": "ALER-T-1", "关联指标": "METR-0005", "比较运算符": "greater_than", "阈值数值": "90", "持续周期数": "2"}},
    ).json()
    assert created["ok"] is True
    entry = created["entry"]
    assert entry["status"] == "草稿"
    assert entry["关联指标版本"] is None
    assert _action(client, entry["id"], "生效规则")["ok"] is False
    assert _action(client, entry["id"], "提交规则评审")["ok"] is True
    rejected = _action(client, entry["id"], "生效规则")
    assert rejected["ok"] is False
    assert "已发布" in rejected["message"]
    # 指标发布后规则才能绑定版本生效。
    assert _metric_action(client, 5, "发布指标", 评审人="口径评审组")["ok"] is True
    activated = _action(client, entry["id"], "生效规则")
    assert activated["ok"] is True
    assert activated["entry"]["关联指标版本"] == "METR-0005-v1"
    assert activated["entry"]["连续周期数"] == 0


def test_invalid_threshold_and_operator_are_rejected(client):
    bad = client.post(
        "/api/alertconfig",
        json={"values": {"预警编号": "ALER-T-2", "关联指标": "METR-0001", "比较运算符": "greater_than", "阈值数值": "abc", "持续周期数": "1"}},
    ).json()
    assert bad["ok"] is False
    bad_op = client.post(
        "/api/alertconfig",
        json={"values": {"预警编号": "ALER-T-3", "关联指标": "METR-0001", "比较运算符": "约等于", "阈值数值": "1", "持续周期数": "1"}},
    ).json()
    assert bad_op["ok"] is False


def test_hit_boundary_equal_and_streak_cycles(client):
    created = client.post(
        "/api/alertconfig",
        json={"values": {"预警编号": "ALER-T-4", "关联指标": "METR-0002", "比较运算符": "greater_than_or_equal", "阈值数值": "30", "持续周期数": "2", "静默时段": "01:00-05:00"}},
    ).json()
    entry_id = created["entry"]["id"]
    assert _action(client, entry_id, "提交规则评审")["ok"]
    assert _action(client, entry_id, "生效规则")["ok"]
    first = _trigger(client, entry_id, "30", "2026-11-01", "2026-11-01T08:00:00")
    assert first["ok"] and first["entry"]["连续周期数"] == 1
    second = _trigger(client, entry_id, "30", "2026-11-02", "2026-11-02T08:00:00")
    assert second["ok"] and second["entry"]["连续周期数"] == 2
    assert len(client.get(f"/api/alertconfig/{entry_id}/notifications").json()) == 1
    # 静默时段内不重复通知。
    third = _trigger(client, entry_id, "31", "2026-11-03", "2026-11-03T03:00:00")
    assert third["ok"] and third["entry"]["连续周期数"] == 3
    assert len(client.get(f"/api/alertconfig/{entry_id}/notifications").json()) == 1
    miss = _trigger(client, entry_id, "29", "2026-11-04")
    assert miss["ok"] and miss["entry"]["连续周期数"] == 0


def test_metric_publish_freezes_and_new_version_requires_new_review(client):
    _metric_action(client, 1, "提交口径评审", 计算公式="成功支付笔数 / 支付请求笔数 * 100（修正）")
    published = _metric_action(client, 1, "发布指标", 评审人="口径评审组")
    assert published["ok"] is True
    rule = _rule(client, 1)
    # 已关闭的规则保持关闭，不被换版影响。
    assert rule["status"] == "已关闭"
    assert rule["连续周期数"] == 5
    assert _action(client, 1, "重新评审")["ok"] is False
    # 已生效规则 2 绑定的是 METR-0002-v1，不受影响。
    assert _rule(client, 2)["status"] == "已生效"


def test_metric_offline_freezes_rules_and_retains_history(client):
    frozen = _metric_action(client, 2, "下线指标", 下线原因="口径合并")
    assert frozen["ok"] is True
    rule = _rule(client, 2)
    assert rule["status"] == "已冻结"
    assert rule["连续周期数"] == 1
    assert rule["冻结原因"].startswith("指标已下线")
    detail = client.get("/api/metric/2/detail").json()
    assert detail["metric"]["可用"] is False
    assert detail["metric"]["指标当前状态"] == "已下线"
    assert {row["status"] for row in detail["alerts"]} == {"已冻结"}
    assert detail["summary"]["已生效"] == 0
    # 关闭的规则保持不变。
    history = client.get("/api/alertconfig/2/history").json()
    assert history[-1]["后状态"] == "已冻结"
    assert _trigger(client, 2, "10", "2026-11-05")["ok"] is False
    assert _rule(client, 2)["连续周期数"] == 1


def test_metric_offline_then_restore_needs_new_review_and_alerts_rebind(client):
    assert _metric_action(client, 2, "下线指标", 下线原因="口径合并")["ok"] is True
    blocked = _action(client, 2, "重新评审")
    assert blocked["ok"] is False  # 指标不可用，不能恢复规则
    rollback = _metric_action(client, 2, "回滚版本", 目标版本="METR-0002-v1")
    assert rollback["ok"] is True
    metric = rollback["entry"]
    assert metric["指标当前状态"] == "口径评审中"
    trace = metric["版本追溯"]
    assert trace[-1]["回滚自版本"] == "METR-0002-v1"
    assert trace[-1]["状态"] == "评审中"
    republish = _metric_action(client, 2, "发布指标", 评审人="口径评审组")
    assert republish["ok"] is True
    assert republish["entry"]["已发布版本"] == "METR-0002-v2"
    # 冻结的规则恢复为草稿，重新评审后绑定新的已发布版本。
    assert _action(client, 2, "重新评审")["ok"] is True
    assert _rule(client, 2)["status"] == "草稿"
    assert _action(client, 2, "提交规则评审")["ok"] is True
    activated = _action(client, 2, "生效规则")
    assert activated["ok"] is True
    assert activated["entry"]["关联指标版本"] == "METR-0002-v2"
    assert activated["entry"]["连续周期数"] == 0


def test_activation_with_stale_version_is_rejected(client):
    assert _metric_action(client, 2, "提交口径评审", 计算公式="新版公式")["ok"] is True
    assert _metric_action(client, 2, "发布指标", 评审人="口径评审组")["ok"] is True
    frozen = _rule(client, 2)
    assert frozen["status"] == "已冻结"
    assert _action(client, 2, "重新评审")["ok"] is True
    assert _action(client, 2, "提交规则评审")["ok"] is True
    stale = _action(client, 2, "生效规则", 指标版本="METR-0001-v1")
    assert stale["ok"] is False
    assert "METR-0002-v2" in stale["message"]


def test_single_side_state_never_persists_on_failure(client, monkeypatch):
    from app.services import alertconfig as alert_module

    original = alert_module.freeze_alerts_for_metric

    def boom(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("模拟冻结后写历史失败")

    monkeypatch.setattr(alert_module, "freeze_alerts_for_metric", boom)
    from app.services import metric as metric_module

    monkeypatch.setattr(metric_module, "freeze_alerts_for_metric", boom)
    result = _metric_action(client, 1, "下线指标", 下线原因="测试事务")
    # service 内部事务回滚后异常应按接口契约返回失败而不是半写。
    assert result["ok"] is False
    metric = client.get("/api/metric/1").json()
    assert metric["指标当前状态"] == "已发布"
    assert _rule(client, 2)["status"] == "已生效"


def test_list_pagination_sort_and_version_lookup(client):
    for index in range(6):
        created = client.post(
            "/api/alertconfig",
            json={"values": {"预警编号": f"ALER-P-{index}", "关联指标": "METR-0002", "比较运算符": "less_than", "阈值数值": "30", "持续周期数": "2"}},
        ).json()
        assert created["ok"] is True
        assert _action(client, created["entry"]["id"], "提交规则评审")["ok"] is True
    page1 = client.get("/api/alertconfig", params={"size": 3, "sort_by": "预警编号", "order": "asc"}).json()
    page2 = client.get("/api/alertconfig", params={"size": 3, "page": 2, "sort_by": "预警编号", "order": "asc"}).json()
    assert page1["total"] == page2["total"] == 9
    ids = [row["预警编号"] for row in page1["items"] + page2["items"]]
    assert ids == sorted(ids)
    assert page2["items"][0]["序号"] == 4
    target = client.get("/api/alertconfig", params={"metric_version": "METR-0001-v1"}).json()
    assert [row["预警编号"] for row in target["items"]] == ["ALER-2026-071"]
    assert target["items"][0]["序列位置"] == "第 1 / 共 1 条"
    bad = client.get("/api/alertconfig", params={"sort_by": "不存在"})
    assert bad.status_code == 400


def test_metric_detail_and_list_status_agree(client):
    metric = client.get("/api/metric/1").json()
    detail = client.get("/api/metric/1/detail").json()
    assert metric["指标当前状态"] == detail["metric"]["指标当前状态"]
    rule = _rule(client, 1)
    assert rule["指标状态"] == metric["指标当前状态"]
    assert detail["alerts"][0]["指标状态"] == rule["指标状态"]
    _metric_action(client, 1, "下线指标", 下线原因="测试")
    detail_after = client.get("/api/metric/1/detail").json()
    assert detail_after["metric"]["指标当前状态"] == "已下线"
    assert _rule(client, 1)["指标状态"] == "已下线"
    assert detail_after["alerts"][0]["status"] == "已关闭"


def test_no_two_versions_of_same_metric_stay_active(client):
    # 指标 2 上再登记一条规则并生效（v1）；换版后只允许新版本上的规则生效。
    created = client.post(
        "/api/alertconfig",
        json={"values": {"预警编号": "ALER-T-5", "关联指标": "METR-0002", "比较运算符": "less_than", "阈值数值": "20", "持续周期数": "1"}},
    ).json()
    rule_id = created["entry"]["id"]
    assert _action(client, rule_id, "提交规则评审")["ok"]
    activated = _action(client, rule_id, "生效规则")
    assert activated["ok"] and activated["entry"]["关联指标版本"] == "METR-0002-v1"
    assert _metric_action(client, 2, "提交口径评审", 计算公式="v2 公式")["ok"]
    assert _metric_action(client, 2, "发布指标", 评审人="口径评审组")["ok"]
    active = client.get("/api/alertconfig", params={"status": "已生效"}).json()["items"]
    versions = {(row["关联指标ID"], row["关联指标版本"]) for row in active}
    assert all(version == "METR-0002-v2" for metric, version in versions if metric == 2)
    assert not any(version == "METR-0002-v1" for _, version in versions)
    frozen = _rule(client, rule_id)
    assert frozen["status"] == "已冻结"
    # 冻结规则不能直接生效：恢复为草稿后必须重新提交评审。
    assert _action(client, rule_id, "生效规则")["ok"] is False
    assert _action(client, rule_id, "重新评审")["ok"] is True
    assert _rule(client, rule_id)["status"] == "草稿"
    assert _action(client, rule_id, "提交规则评审")["ok"] is True
    rebound = _action(client, rule_id, "生效规则")
    assert rebound["ok"] is True
    assert rebound["entry"]["关联指标版本"] == "METR-0002-v2"


def test_trace_links_alert_state_and_metric_version(client):
    trace = client.get("/api/alertconfig/1/trace").json()
    assert trace["rule"]["预警编号"] == "ALER-2026-071"
    assert trace["binding"]["关联指标版本"] == "METR-0001-v1"
    assert trace["binding"]["版本一致"] is True
    assert trace["metric"]["已发布版本"] == "METR-0001-v1"
    assert [row["事件"] for row in trace["history"]][0] == "关闭规则"
    assert len(trace["notifications"]) == 2
    # 关闭之后触发被拒绝，通知与历史不再增长。
    assert _trigger(client, 1, "99.99", "2026-10-12")["ok"] is False
    assert len(client.get("/api/alertconfig/1/notifications").json()) == 2
    assert len(client.get("/api/alertconfig/1/history").json()) == 1


def test_concurrent_threshold_updates_only_one_wins(client):
    assert _action(client, 3, "撤回评审")["ok"] is True
    rule = _rule(client, 3)
    results = []

    def update(value):
        response = client.post(
            "/api/alertconfig/3",
            json={"values": {"阈值数值": value, "expected_rule_version": rule["规则版本"]}},
        )
        results.append(response.json()["ok"])

    threads = [threading.Thread(target=update, args=(str(10 + index),)) for index in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert results.count(False) == 4
    assert _rule(client, 3)["阈值数值"] in {str(10 + index) for index in range(5)}


def test_closing_stops_further_notifications(client):
    created = client.post(
        "/api/alertconfig",
        json={"values": {"预警编号": "ALER-T-6", "关联指标": "METR-0001", "比较运算符": "greater_than", "阈值数值": "90", "持续周期数": "1"}},
    ).json()
    rule_id = created["entry"]["id"]
    assert _action(client, rule_id, "提交规则评审")["ok"]
    assert _action(client, rule_id, "生效规则")["ok"]
    assert _trigger(client, rule_id, "95", "2026-11-01")["entry"]["连续周期数"] == 1
    assert len(client.get(f"/api/alertconfig/{rule_id}/notifications").json()) == 1
    assert _action(client, rule_id, "关闭规则")["ok"]
    closed = _rule(client, rule_id)
    assert closed["status"] == "已关闭"
    assert closed["连续周期数"] == 1
    assert closed["关闭前连续周期"] == 1
    assert _trigger(client, rule_id, "99", "2026-11-02")["ok"] is False
    assert len(client.get(f"/api/alertconfig/{rule_id}/notifications").json()) == 1
    assert _rule(client, rule_id)["连续周期数"] == 1


def test_legacy_single_side_state_is_reconciled_on_read(client):
    # 模拟历史脏数据：指标已下线，规则却仍显示已生效且保留旧周期。
    metric = store.find("metric", 2)
    metric["发布状态"] = "已下线"
    metric["status"] = "已下线"
    rule = store.find("alertconfig", 2)
    rule["status"] = "已生效"
    rule["连续周期数"] = 3
    rule["连续周期"] = 3
    listed = client.get("/api/alertconfig", params={"keyword": "ALER-2026-072"}).json()["items"][0]
    assert listed["status"] == "已冻结"
    assert listed["连续周期数"] == 3  # 旧周期原样保留
    assert listed["指标状态"] == "已下线"
    detail = client.get("/api/metric/2/detail").json()
    assert detail["metric"]["可用"] is False
    assert {row["status"] for row in detail["alerts"]} == {"已冻结"}
    assert detail["summary"]["已生效"] == 0
    history = client.get("/api/alertconfig/2/history").json()
    assert history[-1]["事件"] == "冻结规则"
    # 再次读取不会重复补历史，保证幂等。
    client.get("/api/alertconfig/2")
    assert len(client.get("/api/alertconfig/2/history").json()) == len(history)


def test_pagination_positions_stay_stable_for_same_metric_version(client):
    for index in range(4):
        created = client.post(
            "/api/alertconfig",
            json={"values": {"预警编号": f"ALER-S-{index}", "关联指标": "METR-0001", "比较运算符": "greater_than", "阈值数值": "9", "持续周期数": "1"}},
        ).json()
        assert created["ok"]
        rule_id = created["entry"]["id"]
        assert _action(client, rule_id, "提交规则评审")["ok"]
        assert _action(client, rule_id, "生效规则")["ok"]
    first = client.get("/api/alertconfig", params={"metric_version": "METR-0001-v1", "size": 3, "sort_by": "预警编号"}).json()
    second = client.get("/api/alertconfig", params={"metric_version": "METR-0001-v1", "size": 3, "page": 2, "sort_by": "预警编号"}).json()
    # 种子数据里 ALER-2026-071 也绑定 METR-0001-v1，合计 5 条。
    assert first["total"] == second["total"] == 5
    positions = [row["序列位置"] for row in first["items"] + second["items"]]
    assert positions == [f"第 {index} / 共 5 条" for index in range(1, 6)]
    assert [row["序号"] for row in first["items"] + second["items"]] == [1, 2, 3, 4, 5]
    # 列表接口不会把序号写回仓库，规则详情里没有分页残留字段。
    assert "序号" not in client.get("/api/alertconfig/1").json()


def test_duplicate_cycle_is_not_double_counted(client):
    created = client.post(
        "/api/alertconfig",
        json={"values": {"预警编号": "ALER-T-7", "关联指标": "METR-0001", "比较运算符": "greater_than", "阈值数值": "90", "持续周期数": "3"}},
    ).json()
    rule_id = created["entry"]["id"]
    assert _action(client, rule_id, "提交规则评审")["ok"]
    assert _action(client, rule_id, "生效规则")["ok"]
    first = _trigger(client, rule_id, "95", "2026-11-01")
    assert first["ok"] and first["entry"]["连续周期数"] == 1
    again = _trigger(client, rule_id, "95", "2026-11-01")
    assert again["ok"] is False
    assert "未重复累计" in again["message"]
    assert _rule(client, rule_id)["连续周期数"] == 1
