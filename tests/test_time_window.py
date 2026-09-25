"""限时送达复核（时间展开网络）逻辑测试。

覆盖业务约束：

* 服务端先按既有规则重新审计草稿，审计不通过则不展开时间层；
* 每分钟展开为有向时间层：管段容量按分钟生效、气体可在汇合节点等待、
  超过截止时刻到达的流量不计入；
* 以源头逐分钟持续产生的总量为目标，截止时刻（``t=W``）恰好到达计入；
* 仅全部应排量按时到达才通过；
* 失败时时间层最小割可独立复核：跨割供给边 + 跨层管段容量之和恰等于
  实际送达量（最大流 / 最小割定理），并给出首个受限时刻。
"""
import pytest

from app.flow import NetworkValidationError, review_time_window


def _direct(required=50, cap=100, duration=1, maintainable=False):
    return {
        "source": "S",
        "sink": "T",
        "nodes": [],
        "edges": [
            {"id": "E1", "from": "S", "to": "T", "capacity": cap,
             "maintainable": maintainable, "duration": duration}
        ],
        "required_flow": required,
    }


def test_all_on_time_when_capacity_and_window_suffice():
    """时长 1 分钟、容量充足：每分钟产出都在下一分钟送达，全部按时。"""
    r = review_time_window(**_direct(duration=1), window_minutes=10)
    assert r["passed"] is True
    assert r["stage"] == "time_window"
    assert r["target_total"] == 500
    assert r["delivered_total"] == 500
    assert r["shortage"] == 0
    assert r["failure"] is None
    # t=0 不可能有送达（时长为正整数）；送达发生在 t=1…10
    assert [d["minute"] for d in r["deliveries"]] == list(range(1, 11))
    assert all(d["delivered"] == 50 for d in r["deliveries"])
    # 审计结果一并回传
    assert r["audit"]["passed"] is True


def test_arrival_exactly_at_deadline_counts():
    """t=0 生产、时长 1、窗口 1：恰在截止时刻 t=1 到达，应计入。"""
    r = review_time_window(**_direct(duration=1), window_minutes=1)
    assert r["passed"] is True
    assert r["delivered_total"] == 50
    assert r["deliveries"] == [{"minute": 1, "delivered": 50.0}]


def test_late_minutes_production_cannot_arrive_is_not_false_safe():
    """时长 2、窗口 5：仅 t=0…3 的产出可在 ≤5 到达（200/250）。

    第 4 分钟产出的 50 单位无论如何都会超窗，必须判为不通过——
    气体不能只因“还在途中”就被当作安全送达。
    """
    r = review_time_window(**_direct(duration=2), window_minutes=5)
    assert r["passed"] is False
    assert r["target_total"] == 250
    assert r["delivered_total"] == 200
    assert r["shortage"] == 50
    f = r["failure"]
    assert f["stage"] == "time_window"
    # 第 4 分钟源头完全无法注入（滞留 / 无法按时出发）
    assert f["stranded_from_minute"] == 4
    assert f["first_constrained_minute"] == 4
    # 第 4 分钟产出量 0 也直接体现在逐分钟生产中
    assert r["production"][4]["produced"] == 0.0
    # 送达分钟为 2,3,4,5
    assert [d["minute"] for d in r["deliveries"]] == [2, 3, 4, 5]


def test_waiting_at_junction_allows_later_departure():
    """气体可在汇合节点等待：S→A(1)→T(总时长 3)，W=5。

    t=0…2 的产出经 A 等待后分别于 t=3,4,5 送达。
    """
    r = review_time_window(
        source="S", sink="T", nodes=["A"],
        edges=[
            {"id": "E1", "from": "S", "to": "A", "capacity": 100,
             "maintainable": False, "duration": 1},
            {"id": "E2", "from": "A", "to": "T", "capacity": 100,
             "maintainable": False, "duration": 2},
        ],
        required_flow=50, window_minutes=5,
    )
    assert r["passed"] is False
    assert r["delivered_total"] == 150  # t=0,1,2 各 50
    assert [d["minute"] for d in r["deliveries"]] == [3, 4, 5]
    _assert_cut_identity(r)


def test_capacity_per_minute_bottleneck_across_layers():
    """静态审计通过，但按分钟生效的容量在时间层中构成跨层瓶颈。

    直路 S→T（d=1, cap=30）+ 长路 S→A→T（各 d=1, cap=100），
    要求 60/分、窗口 3：静态最大流 130 审计通过；但第 3 分钟（t=2）
    出发时长路需 t=3 才到达已超窗，仅剩直路 30，出现跨层饱和割管段。
    """
    r = review_time_window(
        source="S", sink="T", nodes=["A"],
        edges=[
            {"id": "D", "from": "S", "to": "T", "capacity": 30,
             "maintainable": False, "duration": 1},
            {"id": "L1", "from": "S", "to": "A", "capacity": 100,
             "maintainable": False, "duration": 1},
            {"id": "L2", "from": "A", "to": "T", "capacity": 100,
             "maintainable": False, "duration": 1},
        ],
        required_flow=60, window_minutes=3,
    )
    assert r["passed"] is False
    # t=0,1：直路30+长路30=60；t=2：只有直路30 能在 t=3 到 => 150
    assert r["delivered_total"] == 150
    assert r["target_total"] == 180
    cut = _assert_cut_identity(r)
    # 直路在 t=2 出发的实例是饱和跨层割管段
    direct_cuts = [p for p in cut["cut_pipe_edges"] if p["edge_id"] == "D"]
    assert any(p["departure_minute"] == 2 and p["saturated"] for p in direct_cuts)
    for p in cut["cut_pipe_edges"]:
        assert p["flow"] <= p["capacity"] + 1e-9
    # 首个受限时刻为第 2 分钟（该分钟只能送出 30 < 60）
    assert r["failure"]["first_constrained_minute"] == 2


def test_edge_time_segment_flows_within_capacity_and_conserve():
    """管段—时段流量逐条不超容量，且源头总注入 == 总送达（流守恒）。"""
    r = review_time_window(
        source="S", sink="T", nodes=["A"],
        edges=[
            {"id": "E1", "from": "S", "to": "A", "capacity": 60,
             "maintainable": False, "duration": 1},
            {"id": "E2", "from": "A", "to": "T", "capacity": 100,
             "maintainable": False, "duration": 1},
        ],
        required_flow=50, window_minutes=4,
    )
    for row in r["edge_flows"]:
        assert row["flow"] <= row["capacity"] + 1e-9
        assert row["arrival_minute"] == row["departure_minute"] + row["duration"]
        assert row["arrival_minute"] <= 4
    produced = sum(p["produced"] for p in r["production"])
    assert abs(produced - r["delivered_total"]) < 1e-6


def test_audit_rerun_first_and_failure_short_circuits():
    """审计未通过（可检修单管失效后断流）时不展开时间层。"""
    r = review_time_window(**_direct(maintainable=True, duration=1),
                           window_minutes=10)
    assert r["passed"] is False
    assert r["stage"] == "audit_failed"
    assert r["time_window"] is None
    assert r["failure"] is None
    # 原有审计失败证据透传
    assert r["audit"]["passed"] is False
    assert r["audit"]["failure"]["stage"] == "single_failure"


def _assert_cut_identity(result) -> dict:
    """复核时间层最小割：容量构成之和 == 实际送达量，且方向正确。"""
    cut = result["failure"]["cut"]
    source_labels = {n["label"] for n in cut["source_side_time_nodes"]}
    sink_labels = {n["label"] for n in cut["sink_side_time_nodes"]}
    # 超级源在源侧、每个割边起点在源侧终点在焚烧端侧
    for p in cut["cut_pipe_edges"]:
        assert p["from_time_node"] in source_labels
        assert p["to_time_node"] in sink_labels
    manual = (
        sum(s["capacity"] for s in cut["cut_supply_edges"])
        + sum(p["capacity"] for p in cut["cut_pipe_edges"])
    )
    assert abs(manual - cut["capacity"]) < 1e-6
    # 最大流 / 最小割定理：割容量恰为实际送达总量
    assert abs(cut["capacity"] - cut["delivered_total"]) < 1e-6
    assert abs(cut["capacity"] - result["delivered_total"]) < 1e-6
    # 时态节点两分组互不相交且覆盖全部 (节点 × 0…W 层)
    assert not (source_labels & sink_labels)
    return cut


@pytest.mark.parametrize(
    "extra, needle",
    [
        ({"window_minutes": 0}, "正整数"),
        ({"window_minutes": -2}, "正整数"),
        ({"window_minutes": 3.0}, "正整数"),
        ({"window_minutes": "5"}, "正整数"),
        ({"window_minutes": True}, "正整数"),
    ],
)
def test_invalid_window_rejected(extra, needle):
    with pytest.raises(NetworkValidationError) as exc:
        review_time_window(**_direct(duration=1), **extra)
    assert needle in str(exc.value)


@pytest.mark.parametrize("bad_duration", [0, -1, 2.0, "3", None, False])
def test_invalid_duration_rejected(bad_duration):
    edges = [{"from": "S", "to": "T", "capacity": 100,
              "maintainable": False, "duration": bad_duration}]
    with pytest.raises(NetworkValidationError) as exc:
        review_time_window(source="S", sink="T", nodes=[], edges=edges,
                           required_flow=50, window_minutes=5)
    assert "输送时长" in str(exc.value) or "正整数" in str(exc.value)


def test_missing_duration_key_rejected():
    edges = [{"from": "S", "to": "T", "capacity": 100, "maintainable": False}]
    with pytest.raises(NetworkValidationError):
        review_time_window(source="S", sink="T", nodes=[], edges=edges,
                           required_flow=50, window_minutes=5)


def test_network_validation_still_applies():
    """节点引用不存在等既有校验在限时复核中同样生效。"""
    edges = [{"from": "S", "to": "幽灵", "capacity": 100,
              "maintainable": False, "duration": 1}]
    with pytest.raises(NetworkValidationError) as exc:
        review_time_window(source="S", sink="T", nodes=[], edges=edges,
                           required_flow=50, window_minutes=5)
    assert "未在节点中定义" in str(exc.value)


def test_direction_still_enforced_in_time_layers():
    """反向管段不能把气送到焚烧端：送达量为 0，审计阶段即失败。"""
    edges = [{"from": "T", "to": "S", "capacity": 100,
              "maintainable": False, "duration": 1}]
    r = review_time_window(source="S", sink="T", nodes=[], edges=edges,
                           required_flow=1, window_minutes=3)
    assert r["stage"] == "audit_failed"
