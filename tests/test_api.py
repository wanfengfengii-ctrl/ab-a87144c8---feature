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


# --------------------------------------------------------------------------- #
# 限时窗口复核 POST /api/time-window-review
# --------------------------------------------------------------------------- #

TW_PASS = {
    "source": "S",
    "sink": "T",
    "required_flow": 95,
    "relief_flow": 10,
    "window_minutes": 3,
    "nodes": [],
    "edges": [
        {"id": "E1", "from": "S", "to": "T", "capacity": 100, "maintainable": True, "transit_minutes": 1},
        {"id": "E2", "from": "S", "to": "T", "capacity": 100, "maintainable": True, "transit_minutes": 1},
    ],
}


def test_time_window_pass():
    r = client.post("/api/time-window-review", json=TW_PASS)
    assert r.status_code == 200
    body = r.json()
    assert body["audit_passed"] is True
    assert body["window_passed"] is True
    assert body["review_stage"] == "window"
    assert body["delivered_flow"] == 30 == body["total_target_flow"]
    assert body["shortfall_flow"] == 0
    assert body["bottleneck"] is None
    # 管段—时段明细：2 条直达管段，输送时长各 1 分钟
    assert len(body["edge_periods"]) == 2
    for e in body["edge_periods"]:
        assert e["transit_minutes"] == 1
        assert all(p["flow"] <= p["capacity"] for p in e["periods"])
    # 服务标识沿用既有约定
    assert body["service"] == "flare-audit"


def test_time_window_capacity_bottleneck_returns_temporal_cut():
    # 两条直达管段分钟容量各 30；审计要求 50（正常网络 60 达标，不模拟
    # 单失效），但事故初每分钟持续泄压 100：第 1 分钟末仅到 60，即受限。
    payload = {
        "source": "S", "sink": "T", "required_flow": 50,
        "relief_flow": 100, "window_minutes": 2,
        "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": 30, "maintainable": False, "transit_minutes": 1},
            {"id": "E2", "from": "S", "to": "T", "capacity": 30, "maintainable": False, "transit_minutes": 1},
        ],
    }
    r = client.post("/api/time-window-review", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["window_passed"] is False
    b = body["bottleneck"]
    # 第 1 分钟末最多送达 60（两条干线各 30），应到 100
    assert b["first_restricted_minute"] == 1
    assert b["cumulative_delivered_flow"] == 60
    cut = b["cut"]
    # 最大流/最小割可复核：割容量恰等于累计按时送达量
    assert cut["capacity"] == 60
    assert {"node": "S", "minute": 0} in cut["source_side_temporal_nodes"]
    assert {"node": "T", "minute": 1} in cut["sink_side_temporal_nodes"]
    assert len(cut["cross_layer_edges"]) == 2
    for x in cut["cross_layer_edges"]:
        assert x["capacity"] == 30 and x["flow"] == 30
        assert x["from_temporal"]["minute"] == 0
        assert x["to_temporal"]["minute"] == 1


def test_time_window_pure_delay_not_judged_safe():
    payload = dict(TW_PASS, window_minutes=1,
                   edges=[
                       {"id": "E1", "from": "S", "to": "T", "capacity": 1000,
                        "maintainable": False, "transit_minutes": 3},
                   ],
                   nodes=[])
    r = client.post("/api/time-window-review", json=payload)
    body = r.json()
    assert body["window_passed"] is False
    assert body["delivered_flow"] == 0
    b = body["bottleneck"]
    assert b["first_restricted_minute"] == 1
    # 超窗未展开管段时段用于定位延迟
    assert [(x["departure_minute"], x["arrival_minute"])
            for x in b["out_of_window_edges"]] == [(0, 3)]


def test_time_window_audit_failure_short_circuits():
    # required_flow=999 使审计不过：不展开时间层，回传审计失败
    payload = dict(TW_PASS, required_flow=999)
    r = client.post("/api/time-window-review", json=payload)
    body = r.json()
    assert r.status_code == 200
    assert body["audit_passed"] is False
    assert body["window_passed"] is False
    assert body["review_stage"] == "audit"
    assert body["audit"]["passed"] is False
    assert "delivered_flow" not in body


def test_time_window_missing_transit_400():
    payload = dict(TW_PASS)
    payload["edges"] = [dict(e) for e in TW_PASS["edges"]]
    del payload["edges"][0]["transit_minutes"]
    r = client.post("/api/time-window-review", json=payload)
    assert r.status_code == 400
    assert "输送时长" in r.json()["error"]
    assert r.json()["field"] == "edges[0].transit_minutes"


def test_time_window_bad_window_param_400():
    r = client.post("/api/time-window-review", json=dict(TW_PASS, window_minutes=0))
    assert r.status_code == 400
    assert "限时窗口" in r.json()["error"]


def test_time_window_non_json_body_400():
    r = client.post("/api/time-window-review", content=b"nope",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
