"""最大流引擎与审计逻辑测试。

重点验证业务约束：
* 管段容量是上限，方向不可逆向；
* 用流量而非路径条数下结论（共享瓶颈场景）；
* 每个单点失效情景在独立残余网络上求解；
* 失败时按录入顺序返回首条失效管段，且割集容量 == 该情景最大流。
"""
import pytest

from app.flow import NetworkValidationError, audit_network


def _base_edges():
    """两条 100 干线并联：S→A→T 与 S→B→T。"""
    return [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 100, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 100, "maintainable": True},
    ]


def test_normal_and_each_single_failure_independent():
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=_base_edges(), required_flow=95)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 200
    assert len(r["scenarios"]) == 4
    # 任一干线管段失效后仍剩 100（独立求解，不串流量）
    assert all(s["max_flow"] == 100 and s["meets"] for s in r["scenarios"])
    assert r["failure"] is None


def test_shared_bottleneck_not_path_count():
    """两条路径共享 50 瓶颈：路径数=2 但最大流只有 50。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": False},
        {"id": "E2", "from": "S", "to": "B", "capacity": 100, "maintainable": False},
        {"id": "E3", "from": "A", "to": "C", "capacity": 100, "maintainable": False},
        {"id": "E4", "from": "B", "to": "C", "capacity": 100, "maintainable": False},
        {"id": "E5", "from": "C", "to": "T", "capacity": 50, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B", "C"],
                      edges=edges, required_flow=60)
    assert r["normal"]["max_flow"] == 50
    assert r["passed"] is False
    # 无可检修管段时，正常网络不达标也要报失败
    assert r["failure"]["stage"] == "normal"
    assert r["failure"]["cut"]["capacity"] == 50


def test_first_failing_edge_by_input_order_and_cut():
    """首条失效管段按录入顺序；割集容量等于该情景最大流。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100, "maintainable": True},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100, "maintainable": True},
        {"id": "E3", "from": "S", "to": "B", "capacity": 90, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 90, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=95)
    assert r["passed"] is False
    f = r["failure"]
    assert f["stage"] == "single_failure"
    assert f["position"] == 1 and f["edge_id"] == "E1"
    assert f["max_flow"] == 90
    cut = f["cut"]
    assert cut["capacity"] == 90 == f["max_flow"]          # 最大流 = 最小割
    assert "S" in cut["source_side_nodes"]
    assert "T" in cut["sink_side_nodes"]
    assert len(cut["cut_edges"]) >= 1
    # 割边方向必须源侧 → 焚烧端侧
    for e in cut["cut_edges"]:
        assert e["from"] in cut["source_side_nodes"]
        assert e["to"] in cut["sink_side_nodes"]
    # 情景表仍按录入顺序完整列出
    assert [s["position"] for s in r["scenarios"]] == [1, 2, 3, 4]
    assert r["scenarios"][0]["meets"] is False
    assert r["scenarios"][1]["meets"] is False
    assert r["scenarios"][2]["meets"] is True


def test_direction_is_enforced():
    """方向不可逆向：T→S 的边不能用来从 S 导流到 T。"""
    edges = [
        {"id": "E1", "from": "T", "to": "S", "capacity": 100, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=1)
    assert r["normal"]["max_flow"] == 0
    assert r["passed"] is False


def test_capacity_is_upper_bound_parallel_edges_sum():
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 30, "maintainable": True},
        {"id": "E2", "from": "S", "to": "T", "capacity": 70, "maintainable": True},
    ]
    # 正常网络：并联容量相加 = 100
    r = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=100)
    assert r["normal"]["max_flow"] == 100
    assert r["normal"]["meets"] is True
    # 但任一可检修管段失效后只剩另一条，整体审计不得通过
    assert r["passed"] is False
    # 要求 71：移除第 1 条(30) 后只剩 70，按录入顺序首条失效即 #1
    r2 = audit_network(source="S", sink="T", nodes=[], edges=edges, required_flow=71)
    assert r2["passed"] is False
    assert r2["failure"]["position"] == 1
    assert r2["failure"]["max_flow"] == 70


def test_non_maintainable_edges_not_simulated():
    edges = _base_edges() + [
        {"id": "E5", "from": "A", "to": "B", "capacity": 10, "maintainable": False},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=95)
    assert len(r["scenarios"]) == 4  # E5 不参与失效模拟
    assert all(s["edge_id"] != "E5" for s in r["scenarios"])


def test_junction_nodes_and_source_sink_auto_registered():
    """泄压源/焚烧端不必出现在 nodes 列表中。"""
    r = audit_network(source="S", sink="T", nodes=[],
                      edges=[{"from": "S", "to": "T", "capacity": 5, "maintainable": False}],
                      required_flow=5)
    assert r["passed"] is True
    assert r["normal"]["max_flow"] == 5


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        (dict(source="S", sink="S", nodes=[], edges=[], required_flow=1), "不能是同一节点"),
        (dict(source="S", sink="T", nodes=["A", "A"], edges=[], required_flow=1), "重复"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "X", "capacity": 1}], required_flow=1), "未在节点中定义"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "X", "to": "T", "capacity": 1}], required_flow=1), "未在节点中定义"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "S", "capacity": 1}], required_flow=1), "起点和终点不能相同"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": 0}], required_flow=1), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": -3}], required_flow=1), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[{"from": "S", "to": "T", "capacity": "大"}], required_flow=1), "正数"),
        (dict(source="S", sink="T", nodes=[], edges=[], required_flow=0), "大于 0"),
        (dict(source="S", sink="T", nodes=[], edges=[], required_flow=-1), "大于 0"),
        (dict(source="", sink="T", nodes=[], edges=[], required_flow=1), "不能为空"),
    ],
)
def test_invalid_inputs_rejected(kwargs, needle):
    with pytest.raises(NetworkValidationError) as exc:
        audit_network(**kwargs)
    assert needle in str(exc.value)


def test_cut_capacity_equals_max_flow_normal():
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 40, "maintainable": True},
        {"id": "E2", "from": "S", "to": "B", "capacity": 60, "maintainable": True},
        {"id": "E3", "from": "A", "to": "T", "capacity": 50, "maintainable": True},
        {"id": "E4", "from": "B", "to": "T", "capacity": 50, "maintainable": True},
    ]
    r = audit_network(source="S", sink="T", nodes=["A", "B"],
                      edges=edges, required_flow=1)
    # S→A 限 40，B→T 限 50，合计 90
    assert r["normal"]["max_flow"] == 90
    assert r["normal"]["cut"]["capacity"] == 90
