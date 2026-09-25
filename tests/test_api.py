"""业务 API 测试：健康检查、审计放行/失败证据、非法输入 400、静态页面。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PASS_PAYLOAD = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "nodes": ["A", "B"],
    "edges": [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True},
    ],
}

FAIL_PAYLOAD = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "nodes": ["A", "B"],
    "edges": [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True},
    ],
}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_page_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "事故导排" in r.text


def test_audit_pass():
    r = client.post("/api/audit", json=PASS_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["normal"]["max_flow"] == 200
    assert len(body["scenarios"]) == 4
    assert all(s["meets"] for s in body["scenarios"])
    assert body["failure"] is None


def test_audit_fail_returns_first_edge_and_cut():
    r = client.post("/api/audit", json=FAIL_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    f = body["failure"]
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 90
    cut = f["cut"]
    assert cut["capacity"] == 90
    assert "S" in cut["source_side_nodes"]
    assert "T" in cut["sink_side_nodes"]
    assert len(cut["cut_edges"]) >= 1


def test_audit_invalid_node_reference_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "不存在", "capacity": 10, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "未在节点中定义" in r.json()["error"]


def test_audit_invalid_capacity_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "T", "capacity": 0, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "大于 0" in r.json()["error"]


def test_audit_invalid_direction_self_loop_400():
    bad = dict(PASS_PAYLOAD)
    bad["edges"] = [{"from": "S", "to": "S", "capacity": 10, "maintainable": True}]
    r = client.post("/api/audit", json=bad)
    assert r.status_code == 400
    assert "起点和终点不能相同" in r.json()["error"]


def test_audit_non_json_body_400():
    r = client.post("/api/audit", content=b"not-json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 限时送达复核 POST /api/time-window-review
# ---------------------------------------------------------------------------

TW_PASS = {
    "source": "S",
    "sink": "T",
    "required_flow": 50,
    "window_minutes": 5,
    "nodes": [],
    "edges": [
        {"id": "E1", "from": "S", "to": "T", "capacity": 100,
         "maintainable": False, "duration": 1},
    ],
}

TW_DELAY = {
    "source": "S",
    "sink": "T",
    "required_flow": 50,
    "window_minutes": 5,
    "nodes": [],
    "edges": [
        {"id": "E1", "from": "S", "to": "T", "capacity": 100,
         "maintainable": False, "duration": 2},
    ],
}


def test_time_window_review_pass():
    r = client.post("/api/time-window-review", json=TW_PASS)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is True
    assert body["stage"] == "time_window"
    assert body["target_total"] == 250
    assert body["delivered_total"] == 250
    assert body["failure"] is None
    assert body["audit"]["passed"] is True
    assert body["service"] == "flare-audit"


def test_time_window_review_late_arrival_fails_with_cut():
    r = client.post("/api/time-window-review", json=TW_DELAY)
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    # t=0…3 产出可在 ≤5 到达：200/250
    assert body["delivered_total"] == 200
    f = body["failure"]
    assert f["first_constrained_minute"] == 4
    cut = f["cut"]
    # 最大流 / 最小割：时间层割容量 == 实际送达量
    assert cut["capacity"] == 200 == cut["delivered_total"]
    assert cut["source_side_time_nodes"]
    assert cut["sink_side_time_nodes"]


def test_time_window_review_audit_failed_short_circuit():
    payload = dict(TW_PASS)
    payload["edges"] = [dict(TW_PASS["edges"][0], maintainable=True)]
    r = client.post("/api/time-window-review", json=payload)
    body = r.json()
    assert body["passed"] is False
    assert body["stage"] == "audit_failed"
    assert body["time_window"] is None
    assert body["audit"]["passed"] is False


def test_time_window_review_invalid_duration_400():
    payload = dict(TW_PASS)
    payload["edges"] = [dict(TW_PASS["edges"][0], duration=0)]
    r = client.post("/api/time-window-review", json=payload)
    assert r.status_code == 400
    assert "正整数" in r.json()["error"]


def test_time_window_review_invalid_window_400():
    payload = dict(TW_PASS, window_minutes=3.5)
    r = client.post("/api/time-window-review", json=payload)
    assert r.status_code == 400
    assert "窗口长度" in r.json()["error"]


def test_time_window_review_non_json_400():
    r = client.post("/api/time-window-review", content=b"xxx",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
