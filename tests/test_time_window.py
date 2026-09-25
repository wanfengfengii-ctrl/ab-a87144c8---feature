"""限时窗口复核（有向时间层展开）测试。

重点验证业务约束：

* 每条管段的正整数**输送时长**把流量推迟到对应时间层，容量按分钟各一份；
* 气体只可在**汇合节点**等待，泄压源逐分钟产生、不囤积，焚烧端到达即终止；
* 抵达时间超出窗口的流量在结构上不进入展开图，绝不计入按时送达；
* 系统以源头逐分钟持续产生的总量为目标，仅当全部应排量按时到达才通过；
* 窗口不够时，首个受限时刻的时间层最小割容量依最大流/最小割定理恰等于
  截至该刻的累计按时送达量，时态节点二分与跨层管段容量均可独立复核；
* 复核前先按既有规则重新审计，审计不过不展开时间层。
"""
import pytest

from app.flow import NetworkValidationError, time_window_review


def _direct(cap=100, transit=1, maintainable=False, eid="E1"):
    return [{"id": eid, "from": "S", "to": "T", "capacity": cap,
             "maintainable": maintainable, "transit_minutes": transit}]


def _review(edges, *, relief=10, window=3, nodes=None, required=10):
    return time_window_review(
        source="S", sink="T", nodes=nodes or [], edges=edges,
        required_flow=required, relief_flow=relief, window_minutes=window,
    )


# --------------------------------------------------------------------------- #
# 送达计时与窗口边界
# --------------------------------------------------------------------------- #

def test_transit_delays_arrival_by_minute():
    """直达管段输送 2 分钟：第 1、2 分钟产量可在第 3 分钟截止前到达，
    最后一分钟产量抵达第 4 分钟，超出窗口不得计入。"""
    r = _review(_direct(cap=100, transit=2), relief=10, window=3)
    assert r["audit_passed"] is True
    assert r["window_passed"] is False
    assert r["total_target_flow"] == 30
    assert r["delivered_flow"] == 20
    assert r["shortfall_flow"] == 10
    # minute 表示抵达层（第 1/2/3 分钟末）
    assert [m["flow"] for m in r["delivered_by_minute"]] == [0, 10, 10]
    # 源头逐分钟注入：前两分钟各 10 入网；第 3 分钟产量 d=2 必超窗，不展开
    assert [m["flow"] for m in r["generated_by_minute"]] == [10, 10, 0]


def test_exact_window_boundary_passes():
    """输送 1 分钟、窗口 3 分钟：三分钟产量分别在第 1/2/3 分钟末到达，
    恰好全部按时，应通过。"""
    r = _review(_direct(cap=100, transit=1), relief=10, window=3)
    assert r["window_passed"] is True
    assert r["delivered_flow"] == 30 == r["total_target_flow"]
    assert [m["flow"] for m in r["delivered_by_minute"]] == [10, 10, 10]
    assert r["bottleneck"] is None


def test_single_minute_window_boundary():
    r = _review(_direct(cap=10, transit=1), relief=10, window=1)
    assert r["window_passed"] is True
    assert r["delivered_flow"] == 10


def test_pure_delay_nothing_arrives_in_window():
    """容量再大，输送 3 分钟也无法在 2 分钟窗口内送达：不得误判安全。"""
    r = _review(_direct(cap=1000, transit=3), relief=10, window=2)
    assert r["window_passed"] is False
    assert r["delivered_flow"] == 0
    assert r["total_target_flow"] == 20
    b = r["bottleneck"]
    assert b["first_restricted_minute"] == 1
    assert b["cumulative_required_flow"] == 10
    assert b["cumulative_delivered_flow"] == 0
    # 纯延迟定位：两个出发层都必然在窗口外到达，按规则未展开
    late = [(x["edge_id"], x["departure_minute"], x["arrival_minute"])
            for x in b["out_of_window_edges"]]
    assert ("E1", 0, 3) in late
    assert ("E1", 1, 4) in late


# --------------------------------------------------------------------------- #
# 容量按分钟生效
# --------------------------------------------------------------------------- #

def test_capacity_applies_per_minute_bottleneck():
    """每分钟产量 100、管段每分钟容量 60：每分钟最多通过 60，
    共享分钟容量不可跨分钟累加。"""
    r = _review(_direct(cap=60, transit=1), relief=100, window=3)
    assert r["window_passed"] is False
    assert r["delivered_flow"] == 180          # 60 × 3
    assert [m["flow"] for m in r["delivered_by_minute"]] == [60, 60, 60]
    b = r["bottleneck"]
    assert b["first_restricted_minute"] == 1  # 第 1 分钟即仅到 60 < 100
    assert b["deficit_flow"] == 40


def test_junction_waiting_allows_peak_shaving():
    """气体可在汇合节点等待：上游分钟 0 即到货，下游每分钟仅 60，
    未送出的部分在汇合节点跨层等待，后续分钟继续送出。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100,
         "maintainable": False, "transit_minutes": 1},
        {"id": "E2", "from": "A", "to": "T", "capacity": 60,
         "maintainable": False, "transit_minutes": 1},
    ]
    r = _review(edges, relief=100, window=3, nodes=["A"])
    assert r["window_passed"] is False
    assert r["delivered_flow"] == 120          # 第 2、3 分钟末各 60
    assert [m["flow"] for m in r["delivered_by_minute"]] == [0, 60, 60]
    periods = {}
    for e in r["edge_periods"]:
        periods[e["edge_id"]] = {
            (p["departure_minute"], p["arrival_minute"]): p["flow"]
            for p in e["periods"]
        }
    # A→T 容量按分钟生效：两个不同时段各 60
    assert periods["E2"][(1, 2)] == 60
    assert periods["E2"][(2, 3)] == 60


def test_source_cannot_wait_unsafely():
    """泄压源不设等待边：某分钟无法送出的产量不会被挪到后一分钟凑数。"""
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 60,
         "maintainable": False, "transit_minutes": 1},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100,
         "maintainable": False, "transit_minutes": 1},
    ]
    r = _review(edges, relief=100, window=2, nodes=["A"])
    # 每分钟最多 60 入网，分钟 0 缺口的 40 不能在源头囤积后补排；
    # 分钟 0 入网的 60 恰在第 2 分钟末送达，分钟 1 入网的 60 抵达 A 时
    # 已到截止层、仍在途中，不得计入。
    assert r["delivered_flow"] == 60
    assert [m["flow"] for m in r["generated_by_minute"]] == [60, 0]
    assert r["shortfall_flow"] == 140


# --------------------------------------------------------------------------- #
# 管段—时段流量明细与守恒
# --------------------------------------------------------------------------- #

def test_edge_period_flows_respect_capacity_and_conserve():
    edges = [
        {"id": "E1", "from": "S", "to": "A", "capacity": 100,
         "maintainable": False, "transit_minutes": 1},
        {"id": "E2", "from": "A", "to": "T", "capacity": 100,
         "maintainable": False, "transit_minutes": 1},
    ]
    r = _review(edges, relief=10, window=3, nodes=["A"])
    assert r["delivered_flow"] == 20
    by_id = {e["edge_id"]: e for e in r["edge_periods"]}
    # 录入顺序保留，每条时段按出发分钟排序
    assert [e["edge_id"] for e in r["edge_periods"]] == ["E1", "E2"]
    for e in r["edge_periods"]:
        assert e["transit_minutes"] >= 1
        assert abs(e["total_flow"] - sum(p["flow"] for p in e["periods"])) < 1e-9
        for p in e["periods"]:
            assert p["flow"] <= p["capacity"] + 1e-9
            assert p["arrival_minute"] - p["departure_minute"] == e["transit_minutes"]
    # 分钟 0 出发经两段各 1 分钟：第 2 分钟末到达 10
    e2_periods = {(p["departure_minute"], p["arrival_minute"]): p["flow"]
                  for p in by_id["E2"]["periods"]}
    assert e2_periods[(1, 2)] == 10
    # 守恒：累计按时送达 == 逐分钟送达之和
    assert abs(r["delivered_flow"]
               - sum(m["flow"] for m in r["delivered_by_minute"])) < 1e-9


# --------------------------------------------------------------------------- #
# 时间层最小割可复核
# --------------------------------------------------------------------------- #

def test_temporal_mincut_is_recomputable():
    r = _review(_direct(cap=60, transit=1), relief=100, window=3)
    b = r["bottleneck"]
    cut = b["cut"]
    # 最大流 / 最小割：割容量 == 该前缀最大流 == 截至该刻累计按时送达量
    assert abs(cut["capacity"] - b["cumulative_delivered_flow"]) < 1e-9
    assert cut["capacity"] == 60

    src = {(n["node"], n["minute"]) for n in cut["source_side_temporal_nodes"]}
    snk = {(n["node"], n["minute"]) for n in cut["sink_side_temporal_nodes"]}
    # 超级源侧必含 (S, 0)，焚烧端侧必含各焚烧端时态节点
    assert ("S", 0) in src
    assert ("T", b["first_restricted_minute"]) in snk
    assert src.isdisjoint(snk)

    total = 0.0
    for x in cut["cross_layer_edges"]:
        total += x["capacity"]
        assert tuple(x["from_temporal"].values()) in src
        assert tuple(x["to_temporal"].values()) in snk
        assert x["arrival_minute"] - x["departure_minute"] == x["transit_minutes"]
    for x in cut["cross_injection_edges"]:
        total += x["capacity"]
        assert tuple(x["temporal_node"].values()) in snk
    # 把跨层管段容量与跨割注入容量相加，应恰为申报割容量
    assert abs(total - cut["capacity"]) < 1e-9
    # 瓶颈管段即分钟容量 60 的 E1 在第 0 分钟出发的副本（已饱和）
    assert [(x["edge_id"], x["departure_minute"], x["flow"])
            for x in cut["cross_layer_edges"]] == [("E1", 0, 60)]


def test_late_arriving_edges_flag_in_transit_volume():
    """容量瓶颈下，受限时刻之后才到达的在途流量单列，供定位延迟。"""
    r = _review(_direct(cap=60, transit=1), relief=100, window=3)
    late = r["bottleneck"]["late_arriving_edges"]
    # 第 1、2 分钟出发的 60 分别在第 2、3 分钟末才到，首受限时刻为 1
    assert [(x["edge_id"], x["departure_minute"], x["arrival_minute"], x["flow"])
            for x in late] == [
                ("E1", 1, 2, 60),
                ("E1", 2, 3, 60),
            ]


# --------------------------------------------------------------------------- #
# 先审计后展开
# --------------------------------------------------------------------------- #

def test_review_reruns_audit_and_short_circuits_on_failure():
    """正常网络容量不足：审计不过，直接返回审计失败，不展开时间层。"""
    r = _review(_direct(cap=5, transit=1), relief=10, window=3, required=999)
    assert r["audit_passed"] is False
    assert r["window_passed"] is False
    assert r["review_stage"] == "audit"
    assert r["relief_flow"] is None and r["window_minutes"] is None
    assert r["audit"]["passed"] is False
    assert "delivered_flow" not in r


def test_review_blocked_by_single_failure_scenario():
    """正常网络达标但某可检修管段失效后不达标：审计不过，限时复核不得继续。"""
    edges = [
        {"id": "E1", "from": "S", "to": "T", "capacity": 100,
         "maintainable": True, "transit_minutes": 1},
    ]
    r = _review(edges, relief=10, window=3, required=50)
    assert r["audit_passed"] is False
    assert r["audit"]["failure"]["stage"] == "single_failure"
    assert "delivered_flow" not in r


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "transit,needle",
    [
        (None, "输送时长"),
        (0, "正整数"),
        (-2, "正整数"),
        (1.5, "正整数"),
        ("2", "正整数"),
        (True, "正整数"),
    ],
)
def test_transit_minutes_must_be_positive_integer(transit, needle):
    edge = {"id": "E1", "from": "S", "to": "T", "capacity": 100,
            "maintainable": False}
    if transit is not None:
        edge["transit_minutes"] = transit
    with pytest.raises(NetworkValidationError) as exc:
        _review([edge])
    assert needle in str(exc.value)


@pytest.mark.parametrize("window", [0, -1, 2.0, "3", False, None])
def test_window_minutes_must_be_positive_integer(window):
    with pytest.raises(NetworkValidationError) as exc:
        _review(_direct(transit=1), window=window)
    assert "限时窗口长度" in str(exc.value)


@pytest.mark.parametrize("relief", [0, -5, "10", True])
def test_relief_flow_must_be_positive_number(relief):
    with pytest.raises(NetworkValidationError) as exc:
        _review(_direct(transit=1), relief=relief)
    assert "泄压流量" in str(exc.value)


def test_float_relief_supported():
    r = _review(_direct(cap=1, transit=1), relief=0.3, window=2, required=0.3)
    assert r["window_passed"] is True
    assert abs(r["delivered_flow"] - 0.6) < 1e-9
