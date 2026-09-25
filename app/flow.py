"""最大流 / 最小割引擎、检修审计与事故初限时窗口复核逻辑。

规则（对应业务要求）：

* 每条管段被视作一条有向边，录入的最大流量即其容量上限；
* 在**正常网络**与**每一条可检修管段单独移除后的残余网络**上，
  分别独立运行最大流（Dinic），互不复用中间流量结果；
* 事故要求的持续排出流量必须在正常网络和每一个单点失效情景下都可达，
  审计才放行；
* 失效时按管段录入顺序返回第一条不达标管段，并依据最大流 / 最小割定理，
  从残余网络给出可复核的源侧割集、焚烧端侧节点及割集容量。

限时窗口复核（审计通过后）：

* 每条管段还须录入**正整数输送时长**（分钟）；事故最初若干分钟内，
  源头每分钟持续产生 ``relief_flow`` 的泄压气体，须在 ``window_minutes``
  截止前送达安全焚烧端；
* 服务端先按既有规则重新审计当前草稿，再把每分钟展开为有向时间层：
  管段容量按分钟生效（每个时间层各一份容量），气体可在汇合节点等待，
  抵达时间超出窗口的流量不计入送达；
* 返回截止时刻前实际按时送达量与“管段—时段”流量明细；
* 窗口不够时，按时间层最小割给出可复核的**首个受限时刻**、源侧与
  焚烧端侧**时态节点**以及跨层管段容量，用于定位延迟或瓶颈。

注意：本模块用“流量”而不是“路径条数”下结论——存在多条路径并不保证
总排量达标，共享瓶颈会限制总流量；限时复核中，滞留在管段内、截止时刻
仍未抵达焚烧端的气体同样不计入安全送达。
"""
from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
from typing import Optional

# 检修网络节点规模通常不大，放宽递归深度以支持较长的增广链。
sys.setrecursionlimit(100_000)

EPS = 1e-9


class NetworkValidationError(ValueError):
    """网络输入无效（节点引用、容量、方向等业务校验失败）。"""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.field = field


@dataclass
class _Edge:
    """Dinic 内部边（带反向残量边索引）。"""

    to: int
    rev: int
    cap: float


class Dinic:
    """容量为非负实数的有向图 Dinic 最大流。"""

    def __init__(self, n: int):
        self.n = n
        self.g: list[list[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: float) -> None:
        fwd = _Edge(to=v, rev=len(self.g[v]), cap=float(cap))
        bak = _Edge(to=u, rev=len(self.g[u]), cap=0.0)
        self.g[u].append(fwd)
        self.g[v].append(bak)

    def _bfs(self, s: int, t: int) -> list[int]:
        level = [-1] * self.n
        level[s] = 0
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > EPS and level[e.to] < 0:
                    level[e.to] = level[u] + 1
                    q.append(e.to)
        return level

    def _dfs(self, u: int, t: int, pushed: float, level: list[int], it: list[int]) -> float:
        if u == t:
            return pushed
        while it[u] < len(self.g[u]):
            e = self.g[u][it[u]]
            if e.cap > EPS and level[e.to] == level[u] + 1:
                got = self._dfs(e.to, t, min(pushed, e.cap), level, it)
                if got > EPS:
                    e.cap -= got
                    self.g[e.to][e.rev].cap += got
                    return got
            it[u] += 1
        return 0.0

    def max_flow(self, s: int, t: int) -> float:
        flow = 0.0
        inf = float("inf")
        while True:
            level = self._bfs(s, t)
            if level[t] < 0:
                return flow
            it = [0] * self.n
            while True:
                pushed = self._dfs(s, t, inf, level, it)
                if pushed <= EPS:
                    break
                flow += pushed

    def reachable_from_source(self, s: int) -> list[bool]:
        """最大流计算后，沿残余容量 > 0 的边做 BFS，得到源侧节点集合。"""
        seen = [False] * self.n
        seen[s] = True
        q = deque([s])
        while q:
            u = q.popleft()
            for e in self.g[u]:
                if e.cap > EPS and not seen[e.to]:
                    seen[e.to] = True
                    q.append(e.to)
        return seen


def _clean_name(raw, field: str) -> str:
    if not isinstance(raw, str):
        raise NetworkValidationError(f"{field} 必须是字符串", field)
    name = raw.strip()
    if not name:
        raise NetworkValidationError(f"{field} 不能为空", field)
    return name


def _finite_positive_number(raw, field: str) -> float:
    import math

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise NetworkValidationError(f"{field} 必须是正数", field)
    value = float(raw)
    if not math.isfinite(value):
        raise NetworkValidationError(f"{field} 必须是有限数值", field)
    if value <= 0:
        raise NetworkValidationError(f"{field} 必须大于 0", field)
    return value


def _positive_int(raw, field: str) -> int:
    """正整数（分钟）：严格要求 Python int，拒绝布尔、浮点与字符串。"""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise NetworkValidationError(f"{field} 必须是正整数", field)
    return raw


def _prepare_network(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
    validate_transit: bool = False,
) -> dict:
    """校验并清洗网络草稿，返回索引表与规范化管段（审计/限时复核共用）。

    管段上的 ``transit_minutes``（输送时长，正整数分钟）为限时复核字段：
    既有检修审计**不读取也不校验**该字段（向后兼容）；仅当
    ``validate_transit=True`` 时才要求每条管段都给出正整数输送时长。
    """
    import math

    source = _clean_name(source, "泄压源")
    sink = _clean_name(sink, "安全焚烧端")
    if source == sink:
        raise NetworkValidationError("泄压源与安全焚烧端不能是同一节点", "sink")

    if isinstance(required_flow, bool) or not isinstance(required_flow, (int, float)):
        raise NetworkValidationError("事故持续排出流量必须是正数", "required_flow")
    required_flow = float(required_flow)
    if not math.isfinite(required_flow) or required_flow <= 0:
        raise NetworkValidationError("事故持续排出流量必须是大于 0 的有限数值", "required_flow")

    if not isinstance(nodes, list):
        raise NetworkValidationError("汇合节点必须是列表", "nodes")

    node_set: set[str] = {source, sink}
    junction_names: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(nodes):
        field = f"nodes[{i}]"
        name = _clean_name(raw, field)
        if name in seen:
            raise NetworkValidationError(f"汇合节点“{name}”重复", field)
        seen.add(name)
        junction_names.append(name)
        node_set.add(name)

    if not isinstance(edges, list):
        raise NetworkValidationError("管段必须是列表", "edges")

    clean_edges: list[dict] = []
    for i, raw in enumerate(edges):
        if not isinstance(raw, dict):
            raise NetworkValidationError(f"第 {i + 1} 条管段格式无效", f"edges[{i}]")
        label = raw.get("id")
        if label is not None and not (isinstance(label, str) and label.strip()):
            label = None
        elif isinstance(label, str):
            label = label.strip()

        u = _clean_name(raw.get("from"), f"第 {i + 1} 条管段起点")
        v = _clean_name(raw.get("to"), f"第 {i + 1} 条管段终点")
        if u not in node_set:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段起点“{u}”未在节点中定义", f"edges[{i}].from"
            )
        if v not in node_set:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段终点“{v}”未在节点中定义", f"edges[{i}].to"
            )
        if u == v:
            raise NetworkValidationError(
                f"第 {i + 1} 条管段起点和终点不能相同（{u}）", f"edges[{i}].to"
            )
        cap = _finite_positive_number(raw.get("capacity"), f"第 {i + 1} 条管段最大流量")
        maintainable = bool(raw.get("maintainable", False))

        transit_raw = raw.get("transit_minutes")
        transit_minutes = None
        if validate_transit:
            if transit_raw is None:
                raise NetworkValidationError(
                    f"第 {i + 1} 条管段缺少正整数输送时长（transit_minutes）",
                    f"edges[{i}].transit_minutes",
                )
            transit_minutes = _positive_int(
                transit_raw, f"第 {i + 1} 条管段输送时长"
            )

        clean_edges.append(
            {
                "index": i,
                "position": i + 1,
                "id": label,
                "from": u,
                "to": v,
                "capacity": cap,
                "maintainable": maintainable,
                "transit_minutes": transit_minutes,
            }
        )

    all_nodes = sorted(node_set)
    return {
        "source": source,
        "sink": sink,
        "required_flow": required_flow,
        "junction_names": junction_names,
        "node_names": all_nodes,
        "index_of": {name: i for i, name in enumerate(all_nodes)},
        "edges": clean_edges,
    }


def audit_network(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
) -> dict:
    """校验输入并执行正常网络 + 全部单点失效情景的最大流审计。

    ``nodes`` 为汇合节点（及其它中间节点）列表；泄压源与安全焚烧端
    自动并入节点集合。``edges`` 每项形如::

        {"id": "E1" | None, "from": "S", "to": "T",
         "capacity": 100.0, "maintainable": True,
         "transit_minutes": 3  # 可选，仅限时复核使用，审计忽略}

    返回可直接 JSON 序列化的审计结论（见模块 docstring 与 README）。
    """
    prepared = _prepare_network(
        source=source, sink=sink, nodes=nodes, edges=edges,
        required_flow=required_flow,
    )
    source = prepared["source"]
    sink = prepared["sink"]
    required_flow = prepared["required_flow"]
    all_nodes = prepared["node_names"]
    index_of = prepared["index_of"]
    clean_edges = prepared["edges"]

    def _solve(removed_index: Optional[int]) -> tuple[float, dict]:
        """在一张**全新**的网络上独立求最大流，并返回流量与最小割证据。"""
        dinic = Dinic(len(all_nodes))
        active = []
        for e in clean_edges:
            if e["index"] == removed_index:
                continue
            dinic.add_edge(index_of[e["from"]], index_of[e["to"]], e["capacity"])
            active.append(e)
        value = dinic.max_flow(index_of[source], index_of[sink])
        side = dinic.reachable_from_source(index_of[source])

        source_side = sorted(all_nodes[k] for k, ok in enumerate(side) if ok)
        sink_side = sorted(all_nodes[k] for k, ok in enumerate(side) if not ok)
        cut_edges = []
        cut_capacity = 0.0
        for e in active:  # 按录入顺序列出，便于复核
            if side[index_of[e["from"]]] and not side[index_of[e["to"]]]:
                cut_edges.append(
                    {
                        "index": e["index"],
                        "position": e["position"],
                        "id": e["id"],
                        "from": e["from"],
                        "to": e["to"],
                        "capacity": _num(e["capacity"]),
                    }
                )
                cut_capacity += e["capacity"]
        return value, {
            "capacity": _num(cut_capacity),
            "source_side_nodes": source_side,
            "sink_side_nodes": sink_side,
            "cut_edges": cut_edges,
        }

    def _meets(value: float) -> bool:
        return value + EPS >= required_flow

    # 1) 正常网络
    normal_value, normal_cut = _solve(None)

    # 2) 每条可检修管段单独临时失效（残余网络独立求解）
    scenarios = []
    failure = None
    for e in clean_edges:
        if not e["maintainable"]:
            continue
        value, cut = _solve(e["index"])
        scenario = {
            "edge_index": e["index"],
            "position": e["position"],
            "edge_id": e["id"],
            "from": e["from"],
            "to": e["to"],
            "capacity": _num(e["capacity"]),
            "max_flow": _num(value),
            "meets": _meets(value),
        }
        scenarios.append(scenario)
        # 按管段录入顺序取首条不达标者
        if failure is None and not _meets(value):
            failure = {
                "stage": "single_failure",
                "edge_index": e["index"],
                "position": e["position"],
                "edge_id": e["id"],
                "from": e["from"],
                "to": e["to"],
                "capacity": _num(e["capacity"]),
                "required_flow": _num(required_flow),
                "max_flow": _num(value),
                "cut": cut,
            }

    # 没有任何可检修管段时，至少正常网络本身必须达标
    if failure is None and not scenarios and not _meets(normal_value):
        failure = {
            "stage": "normal",
            "edge_index": None,
            "position": None,
            "edge_id": None,
            "from": None,
            "to": None,
            "capacity": None,
            "required_flow": _num(required_flow),
            "max_flow": _num(normal_value),
            "cut": normal_cut,
        }

    return {
        "passed": failure is None and _meets(normal_value),
        "required_flow": _num(required_flow),
        "normal": {
            "max_flow": _num(normal_value),
            "meets": _meets(normal_value),
            "cut": normal_cut,
        },
        "scenarios": scenarios,
        "failure": failure,
    }


def _num(x: float) -> float:
    """消除浮点尾差，便于展示与复核（如 0.30000000000000004）。"""
    r = round(float(x), 6)
    return 0.0 if r == 0 else r


# ---------------------------------------------------------------------------
# 限时窗口复核：把“每分钟”展开为有向时间层
# ---------------------------------------------------------------------------
#
# 时间层约定（离散分钟）：
#
#   * 时间层 t = 0,1,…,W（W = window_minutes）；源头在第 0,1,…,W-1 层各注入
#     relief_flow（第 0 层注入对应事故第 1 分钟产生的气体），到第 t 层为止
#     累计应到 relief_flow * t；
#   * 管段 e（容量 c_e、输送时长 d_e 正整数分钟）在每个出发层 s 复制一份：
#     (u, s) → (v, s+d_e)，容量 c_e —— 容量按分钟生效，跨层到达；
#   * 仅**汇合节点**可等待：(v, t-1) → (v, t)，容量取“不可能成为瓶颈”的
#     充分大值（大于全窗口总产量即可）；泄压源逐分钟持续产生、不囤积，
#     焚烧端不设等待边；气体一旦抵达任一焚烧端时态节点即视为已送达，
#     从焚烧端出发的管段不再复制，避免外排后重复计数；
#   * 仅加入 s + d_e ≤ W 的管段副本：到达时间超出窗口的流量根本不进入
#     展开图，因此不可能被计入送达，从结构上杜绝“在途中滞留却被判安全”；
#   * 超级源 SS 逐分钟注入（第 s 层注入第 s+1 分钟产量，s=0,…,W-1）；
#     永久超级汇 TT 通过容量为“充分大值”的收集边 (焚烧端,a)→TT 接入各
#     焚烧端时态节点；第 t 层激活后求一次 SS→TT 最大流，其值恰为
#     时间层 0…t 的累计按时送达量（应到 relief_flow * t），取割时收集边
#     永不饱和、永不跨割；历史送达保留在旧焚烧端时态节点，不会被重复计数。
#
# 增量求解过程中，无法按时送达的产量表现为注入边未饱和（气体未能进入或
# 未能穿越网络），而不是途中虚增的库存；最大流/最小割定理保证受限时刻
# 前缀展开图的最小割容量恰等于截至该刻的累计按时送达量。


class _TimeLayeredNetwork:
    """随分钟逐层激活的时间展开 Dinic 网络（超级源 SS → 超级汇 TT）。"""

    def __init__(self, prepared: dict, relief_flow: float, window_minutes: int):
        self.names: list[str] = prepared["node_names"]
        self.N = len(self.names)
        self.W = window_minutes
        self.index_of = prepared["index_of"]
        self.source = prepared["source"]
        self.sink = prepared["sink"]
        self.edges = prepared["edges"]
        self.relief = float(relief_flow)

        # 层 0..W 共 W+1 个时态副本，其后为超级源 SS、超级汇 TT。
        self.ss = self.N * (window_minutes + 1)
        self.tt = self.ss + 1
        self.dinic = Dinic(self.tt + 1)
        # 充分大容量：大于全窗口总产量，保证等待边/焚烧端收集边既不成为
        # 瓶颈，也不会进入最小割。
        self.big_cap = self.relief * (window_minutes + 1) + 1.0

        # 每条加入 Dinic 的前向边登记一条记录，供事后取流量与最小割。
        self.records: list[dict] = []
        self.delivered = 0.0

    def nid(self, name: str, t: int) -> int:
        return t * self.N + self.index_of[name]

    def _add(self, u: int, v: int, cap: float, record: dict) -> None:
        record["u"] = u
        record["v"] = v
        record["cap0"] = float(cap)
        record["seq"] = len(self.records)
        record["edge_pos"] = len(self.dinic.g[u])
        self.dinic.add_edge(u, v, cap)
        self.records.append(record)

    def _add_injection(self, t: int) -> None:
        self._add(
            self.ss, self.nid(self.source, t), self.relief,
            {"kind": "inject", "departure": t},
        )

    def _add_pipe_copies_arriving_at(self, a: int) -> None:
        """加入所有**抵达层恰为 a** 的管段副本（出发层 s = a - d_e）。

        在求解第 a 层之前调用，保证取第 a 层最小割时图中只含端点不晚于 a
        的边，残余可达集不会越出当前前缀。
        """
        for e in self.edges:
            if e["from"] == self.sink:
                # 已送达焚烧端的气体不再出发，杜绝重复计数。
                continue
            s = a - e["transit_minutes"]
            if s >= 0:
                self._add(
                    self.nid(e["from"], s), self.nid(e["to"], a),
                    e["capacity"],
                    {"kind": "pipe", "edge": e, "departure": s, "arrival": a},
                )

    def activate(self, t: int) -> float:
        """激活第 t 层（t≥1）并求截至该层的累计按时送达量。

        加入的边依次为：汇合节点进入第 t 层的等待边、抵达层恰为 t 的
        管段副本、第 t 层焚烧端收集边、第 t-1 分钟产量注入边。求解时图中
        恰好只有时间层 0…t，注入边共 t 条（累计应到 relief_flow * t）。
        """
        for name in self.names:
            if name == self.sink or name == self.source:
                continue  # 仅汇合节点可等待；焚烧端送达即终止，泄压源逐分钟产生不囤积
            self._add(
                self.nid(name, t - 1), self.nid(name, t), self.big_cap,
                {"kind": "wait", "node": name, "departure": t - 1, "arrival": t},
            )
        self._add_pipe_copies_arriving_at(t)
        # 焚烧端第 t 层 → 超级汇：历史各刻送达在 TT 上累计。
        self._add(
            self.nid(self.sink, t), self.tt, self.big_cap,
            {"kind": "collect", "departure": t},
        )
        self._add_injection(t - 1)
        pushed = self.dinic.max_flow(self.ss, self.tt)
        self.delivered += pushed
        return pushed

    def flow_of(self, record: dict) -> float:
        e = self.dinic.g[record["u"]][record["edge_pos"]]
        f = record["cap0"] - e.cap
        return f if f > EPS else 0.0

    def temporal(self, name: str, t: int) -> dict:
        return {"node": name, "minute": t}


def time_window_review(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
    relief_flow: float,
    window_minutes: int,
) -> dict:
    """检修审计通过后的限时窗口复核。

    服务端**先按既有规则重新审计当前草稿**；审计不通过则直接返回审计结论，
    不展开时间层。审计通过后校验每条管段的正整数输送时长与窗口参数，再做
    分钟级时间展开，返回截止时刻前实际按时送达量、逐分钟注入/送达与
    “管段—时段”流量；窗口不够时附首个受限时刻的时间层最小割证据。
    """
    # 1) 既有规则重新审计（输入校验同样复用，非法草稿返回 400）
    audit = audit_network(
        source=source, sink=sink, nodes=nodes, edges=edges,
        required_flow=required_flow,
    )
    if not audit["passed"]:
        return {
            "audit_passed": False,
            "window_passed": False,
            "review_stage": "audit",
            "relief_flow": None,
            "window_minutes": None,
            "audit": audit,
        }

    # 2) 限时参数与输送时长校验
    relief = _finite_positive_number(relief_flow, "事故初持续泄压流量")
    window = _positive_int(window_minutes, "限时窗口长度")

    # 3) 重新清洗（此次强制每条管段给出正整数输送时长），展开时间层
    prepared = _prepare_network(
        source=source, sink=sink, nodes=nodes, edges=edges,
        required_flow=required_flow, validate_transit=True,
    )
    net = _TimeLayeredNetwork(prepared, relief, window)

    first_restricted = None
    cut_reach = None
    cut_flows: dict[int, float] = {}
    cut_seq_limit = 0
    prefix_delivered_at_restriction = 0.0
    for t in range(1, window + 1):
        net.activate(t)
        cumulative_required = relief * t
        if first_restricted is None and net.delivered + EPS < cumulative_required:
            first_restricted = t
            prefix_delivered_at_restriction = net.delivered
            # 此刻前缀展开图（恰含时间层 0…t）上最大流刚求完：
            # 残余可达集即时间层最小割；同时定格各边当时流量与当时已存在
            # 的边序号上限，避免后续分钟加入的边/增广改道影响复核。
            cut_reach = net.dinic.reachable_from_source(net.ss)
            cut_seq_limit = len(net.records)
            cut_flows = {rec["seq"]: net.flow_of(rec) for rec in net.records}

    # 4) 取最终（全窗口）流量明细
    generated_by_minute: list[dict] = []
    delivered_by_minute: list[dict] = []
    injection_flow = {s: 0.0 for s in range(window)}
    arrival_flow = {a: 0.0 for a in range(1, window + 1)}
    per_edge: dict[int, dict] = {
        e["index"]: {"edge": e, "periods": [], "total": 0.0}
        for e in net.edges
    }

    for rec in net.records:
        if rec["kind"] == "inject":
            injection_flow[rec["departure"]] = net.flow_of(rec)
        elif rec["kind"] == "pipe":
            f = net.flow_of(rec)
            if rec["edge"]["to"] == net.sink:
                arrival_flow[rec["arrival"]] += f
            bucket = per_edge[rec["edge"]["index"]]
            bucket["periods"].append({
                "departure_minute": rec["departure"],
                "arrival_minute": rec["arrival"],
                "capacity": _num(rec["edge"]["capacity"]),
                "flow": _num(f),
            })
            bucket["total"] += f

    for s in range(window):
        generated_by_minute.append({
            "minute": s,
            "flow": _num(injection_flow[s]),
            "target_flow": _num(relief),
        })
    for a in range(1, window + 1):
        delivered_by_minute.append({
            "minute": a,
            "flow": _num(arrival_flow[a]),
        })

    edge_periods = []
    for e in net.edges:  # 保持管段录入顺序
        bucket = per_edge[e["index"]]
        bucket["periods"].sort(key=lambda p: p["departure_minute"])
        edge_periods.append({
            "edge_index": e["index"],
            "position": e["position"],
            "edge_id": e["id"],
            "from": e["from"],
            "to": e["to"],
            "capacity": _num(e["capacity"]),
            "transit_minutes": e["transit_minutes"],
            "periods": bucket["periods"],
            "total_flow": _num(bucket["total"]),
        })

    target_total = relief * window
    delivered_total = net.delivered
    window_passed = first_restricted is None and delivered_total + EPS >= target_total

    result = {
        "audit_passed": True,
        "window_passed": window_passed,
        "review_stage": "window",
        "relief_flow": _num(relief),
        "window_minutes": window,
        "deadline_minute": window,
        "total_target_flow": _num(target_total),
        "delivered_flow": _num(delivered_total),
        "shortfall_flow": _num(max(0.0, target_total - delivered_total)),
        "generated_by_minute": generated_by_minute,
        "delivered_by_minute": delivered_by_minute,
        "edge_periods": edge_periods,
        "audit": {
            "passed": True,
            "normal_max_flow": audit["normal"]["max_flow"],
            "scenario_count": len(audit["scenarios"]),
        },
        "bottleneck": None,
    }

    # 5) 窗口不够：组装可复核的时间层最小割证据
    if first_restricted is not None:
        t = first_restricted
        reach = cut_reach

        # 5.1 前缀（时间层 0…t）时态节点二分
        source_side = []
        sink_side = []
        for layer in range(0, t + 1):
            for name in net.names:
                node = net.temporal(name, layer)
                (source_side if reach[net.nid(name, layer)] else sink_side).append(node)

        # 5.2 正式时间层最小割：仅统计取割时图中存在（抵达层 ≤ t）、
        # 从源侧时态节点跨到焚烧端侧时态节点的边。最大流/最小割定理保证
        # 割容量恰等于该前缀累计按时送达量，可独立复核。
        cross_pipes = []
        cross_injections = []
        cut_capacity = 0.0
        for rec in net.records:
            if rec["seq"] >= cut_seq_limit:
                break  # 仅在受限时刻已存在的前缀边参与该时刻最小割
            f_at_cut = cut_flows.get(rec["seq"], 0.0)
            if rec["kind"] in ("wait", "collect"):
                continue  # 充分大容量边：不会饱和，也不会跨最小割
            if rec["kind"] == "inject":
                if rec["departure"] >= t or reach[rec["v"]]:
                    continue  # 取割时只存在 0…t-1 注入；可达注入层不跨割
                cross_injections.append({
                    "departure_minute": rec["departure"],
                    "capacity": _num(rec["cap0"]),
                    "flow": _num(f_at_cut),
                    "temporal_node": net.temporal(net.source, rec["departure"]),
                })
                cut_capacity += rec["cap0"]
            else:  # pipe（取割时图中管段副本抵达层必 ≤ t）
                if not (reach[rec["u"]] and not reach[rec["v"]]):
                    continue
                e = rec["edge"]
                cross_pipes.append({
                    "edge_index": e["index"],
                    "position": e["position"],
                    "edge_id": e["id"],
                    "from": e["from"],
                    "to": e["to"],
                    "transit_minutes": e["transit_minutes"],
                    "departure_minute": rec["departure"],
                    "arrival_minute": rec["arrival"],
                    "capacity": _num(rec["cap0"]),
                    "flow": _num(f_at_cut),
                    "from_temporal": net.temporal(e["from"], rec["departure"]),
                    "to_temporal": net.temporal(e["to"], rec["arrival"]),
                })
                cut_capacity += rec["cap0"]

        # 5.3 延迟诊断（最终展开解口径，流量均可在最终图上独立复核）：
        # (a) 已承载流量、但抵达层晚于首个受限时刻 t 的跨层管段——
        #     这些气体 t 时刻仍在途中，不能用于解除当刻缺口；
        # (b) 输送时长使其 s+d_e > W、按规则根本未展开的管段时段——
        #     这些分钟即使满载出发也不可能在窗口内抵达（纯延迟定位）。
        late_pipes = []
        for rec in net.records:
            if rec["kind"] != "pipe" or rec["arrival"] <= t:
                continue
            f = net.flow_of(rec)
            if f <= EPS:
                continue
            e = rec["edge"]
            late_pipes.append({
                "edge_index": e["index"],
                "position": e["position"],
                "edge_id": e["id"],
                "from": e["from"],
                "to": e["to"],
                "transit_minutes": e["transit_minutes"],
                "departure_minute": rec["departure"],
                "arrival_minute": rec["arrival"],
                "capacity": _num(rec["cap0"]),
                "flow": _num(f),
                "from_temporal": net.temporal(e["from"], rec["departure"]),
                "to_temporal": net.temporal(e["to"], rec["arrival"]),
            })
        late_pipes.sort(key=lambda x: (x["arrival_minute"], x["position"],
                                       x["departure_minute"]))

        out_of_window_pipes = []
        for e in net.edges:
            if e["from"] == net.sink:
                continue
            for s in range(window):
                arrival = s + e["transit_minutes"]
                if arrival > window:
                    out_of_window_pipes.append({
                        "edge_index": e["index"],
                        "position": e["position"],
                        "edge_id": e["id"],
                        "from": e["from"],
                        "to": e["to"],
                        "transit_minutes": e["transit_minutes"],
                        "departure_minute": s,
                        "arrival_minute": arrival,
                        "capacity": _num(e["capacity"]),
                        "flow": None,  # 未展开：不可计入送达
                        "from_temporal": net.temporal(e["from"], s),
                        "to_temporal": net.temporal(e["to"], arrival),
                    })
        out_of_window_pipes.sort(key=lambda x: (x["arrival_minute"], x["position"],
                                                x["departure_minute"]))

        cumulative_required = relief * t
        result["bottleneck"] = {
            "first_restricted_minute": t,
            "cumulative_required_flow": _num(cumulative_required),
            "cumulative_delivered_flow": _num(prefix_delivered_at_restriction),
            "deficit_flow": _num(cumulative_required - prefix_delivered_at_restriction),
            "cut": {
                # 最大流/最小割定理：割容量恰等于前缀展开图最大流，
                # 即截至该时刻累计按时送达量，可独立复核。
                "capacity": _num(cut_capacity),
                "source_side_temporal_nodes": source_side,
                "sink_side_temporal_nodes": sink_side,
                "cross_layer_edges": cross_pipes,
                "cross_injection_edges": cross_injections,
            },
            # 非割组成部分：仅用于工程师定位延迟环节的在途/超窗管段。
            "late_arriving_edges": late_pipes,
            "out_of_window_edges": out_of_window_pipes,
        }

    return result
