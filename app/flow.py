"""最大流 / 最小割引擎、检修审计与限时送达复核逻辑。

规则（对应业务要求）：

* 每条管段被视作一条有向边，录入的最大流量即其容量上限；
* 在**正常网络**与**每一条可检修管段单独移除后的残余网络**上，
  分别独立运行最大流（Dinic），互不复用中间流量结果；
* 事故要求的持续排出流量必须在正常网络和每一个单点失效情景下都可达，
  审计才放行；
* 失效时按管段录入顺序返回第一条不达标管段，并依据最大流 / 最小割定理，
  从残余网络给出可复核的源侧割集、焚烧端侧节点及割集容量。

限时送达复核（``review_time_window`` / ``POST /api/time-window-review``）：

* 每条管段另录**正整数输送时长**（分钟）；
* 服务端先按上述既有规则重新审计当前草稿，审计通过后才展开时间层；
* 每分钟展开为一个有向时间层：管段容量按分钟生效（每个时间层实例独立
  占有完整容量），气体可在汇合节点等待（同节点相邻分钟间为等待边），
  到达时间超出窗口的流量根本不进入网络，故不得计入；
* 以泄压源逐分钟持续产生的总量为目标，求截止时刻前实际送达焚烧端的
  总量及“管段—出发分钟”流量；仅当全部应排量按时到达才出具通过结论；
* 不通过时按**时间层最小割**给出可复核的首个受限时刻、源侧 / 焚烧端侧
  时态节点与跨层管段容量，供定位延迟或瓶颈。

注意：本模块用“流量”而不是“路径条数”下结论——存在多条路径并不保证
总排量达标，共享瓶颈会限制总流量。
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
    """正整数校验：布尔、浮点（如 1.5）、非数字均拒绝。"""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise NetworkValidationError(f"{field}必须是正整数（分钟）", field)
    if raw <= 0:
        raise NetworkValidationError(f"{field}必须是大于 0 的正整数", field)
    return raw


def clean_network(
    *,
    source,
    sink,
    nodes,
    edges,
    required_flow,
    require_duration: bool = False,
) -> dict:
    """校验并清洗一份管网草稿，返回审计 / 时间层复核共用的规范结构。

    ``edges`` 每项形如::

        {"id": "E1" | None, "from": "S", "to": "T",
         "capacity": 100.0, "maintainable": True, "duration": 3}

    ``duration``（输送时长，正整数分钟）仅在 ``require_duration=True``
    时必填——限时复核需要它，而既有检修审计接口保持向后兼容。
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
        duration = None
        if require_duration:
            duration = _positive_int(
                raw.get("duration"), f"第 {i + 1} 条管段输送时长"
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
                "duration": duration,
            }
        )

    all_nodes = sorted(node_set)
    index_of = {name: i for i, name in enumerate(all_nodes)}
    return {
        "source": source,
        "sink": sink,
        "required_flow": required_flow,
        "junction_names": junction_names,
        "all_nodes": all_nodes,
        "index_of": index_of,
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
         "capacity": 100.0, "maintainable": True}

    返回可直接 JSON 序列化的审计结论（见模块 docstring 与 README）。
    """
    cleaned = clean_network(
        source=source,
        sink=sink,
        nodes=nodes,
        edges=edges,
        required_flow=required_flow,
    )
    source = cleaned["source"]
    sink = cleaned["sink"]
    required_flow = cleaned["required_flow"]
    all_nodes = cleaned["all_nodes"]
    index_of = cleaned["index_of"]
    clean_edges = cleaned["edges"]

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
# 限时送达复核：时间展开（time-expanded）网络
# ---------------------------------------------------------------------------

# 时间层中等待边 / 供给边使用的“足够大”容量：业务上它们不应成为瓶颈。
# 取一个显式的有限大值，避免在图中使用浮点 inf 带来的数值问题。
def _big_capacity(required_flow: float, window: int) -> float:
    return required_flow * (window + 1) + 1.0


def review_time_window(
    *,
    source: str,
    sink: str,
    nodes: list[str],
    edges: list[dict],
    required_flow: float,
    window_minutes: int,
) -> dict:
    """限时窗口送达复核。

    服务端**先按既有规则重新审计当前草稿**；审计不通过时直接返回失败
    （``stage="audit_failed"``），不展开时间层——限时结论只对审计合格
    的管网有意义。

    审计通过后，把每分钟展开为一个有向时间层（详见模块 docstring）：

    * 源头逐分钟（``t = 0 … W-1``）经超级源注入 ``required_flow``，
      故应排目标总量为 ``required_flow × W``；
    * 管段 ``u→v``（时长 ``d``、容量 ``c``）对每个可行出发分钟
      ``t``（``t+d ≤ W``）展开为 ``(u,t)→(v,t+d)``，容量 ``c``
      ——容量按分钟生效，每个时间层实例独立占有完整容量；
      ``t+d > W`` 的实例根本不建边，超窗到达的流量无法进入网络；
      窗口共展开 ``W+1`` 个时间层（``0…W``），截止时刻 ``t=W``
      恰好到达仍计入，更晚则不计；
    * 非焚烧端节点设有等待边 ``(v,t)→(v,t+1)``（无限容量），
      气体可在汇合节点等待；焚烧端不设等待边——到达即计入送达；
    * 每个时态焚烧端 ``(sink,t)`` 直连超级汇，该边流量即第 ``t``
      分钟的实际送达量（``t=0`` 恒为零，因时长为正整数）。

    返回实际送达总量、逐分钟送达量、管段—出发分钟流量，以及未通过时
    可复核的时间层最小割证据。
    """
    window = _positive_int(window_minutes, "窗口长度")
    if not isinstance(edges, list):
        raise NetworkValidationError("管段必须是列表", "edges")

    cleaned = clean_network(
        source=source,
        sink=sink,
        nodes=nodes,
        edges=edges,
        required_flow=required_flow,
        require_duration=True,
    )
    source = cleaned["source"]
    sink = cleaned["sink"]
    required_flow = cleaned["required_flow"]
    all_nodes = cleaned["all_nodes"]
    index_of = cleaned["index_of"]
    clean_edges = cleaned["edges"]

    # 1) 既有规则重新审计（独立计算，不复用任何流量结果）
    audit = audit_network(
        source=source,
        sink=sink,
        nodes=cleaned["junction_names"],
        edges=[
            {
                "id": e["id"],
                "from": e["from"],
                "to": e["to"],
                "capacity": e["capacity"],
                "maintainable": e["maintainable"],
            }
            for e in clean_edges
        ],
        required_flow=required_flow,
    )
    if not audit["passed"]:
        return {
            "passed": False,
            "stage": "audit_failed",
            "window_minutes": window,
            "required_flow": _num(required_flow),
            "target_total": _num(required_flow * window),
            "delivered_total": 0.0,
            "audit": audit,
            "time_window": None,
            "failure": None,
        }

    # 2) 展开时间层（共 W+1 层：t = 0 … W，截止时刻 t=W 恰好到达计入）
    n_nodes = len(all_nodes)
    src_idx = index_of[source]
    sink_idx = index_of[sink]
    n_layers = window + 1

    def vid(node_i: int, t: int) -> int:
        return t * n_nodes + node_i

    super_source = n_nodes * n_layers
    super_sink = super_source + 1
    dinic = Dinic(super_sink + 1)
    big = _big_capacity(required_flow, window)

    # 记录每条“管段时间实例边”的初始容量与邻接表位置，以便事后反推实际流量。
    pipe_records: list[dict] = []

    # 2a) 源头逐分钟持续产生：super_source → (source, t)，t = 0 … W-1
    supply_records = []
    for t in range(window):
        fwd_idx = len(dinic.g[super_source])
        dinic.add_edge(super_source, vid(src_idx, t), required_flow)
        supply_records.append(
            {"t": t, "edge_pos": fwd_idx, "capacity": required_flow}
        )

    # 2b) 管段：(u, t) → (v, t+d)，仅当 t+d ≤ W（截止时刻恰好到达仍计入）
    for e in clean_edges:
        u = index_of[e["from"]]
        v = index_of[e["to"]]
        d = e["duration"]
        for t in range(window - d + 1):
            fwd_idx = len(dinic.g[vid(u, t)])
            dinic.add_edge(vid(u, t), vid(v, t + d), e["capacity"])
            pipe_records.append(
                {
                    "edge_index": e["index"],
                    "position": e["position"],
                    "id": e["id"],
                    "from": e["from"],
                    "to": e["to"],
                    "duration": d,
                    "departure_minute": t,
                    "arrival_minute": t + d,
                    "capacity": e["capacity"],
                    "from_vid": vid(u, t),
                    "edge_pos": fwd_idx,
                }
            )

    # 2c) 汇合 / 中间节点等待：(v, t) → (v, t+1)；焚烧端不等待（到达即送达）
    for ni in range(n_nodes):
        if ni == sink_idx:
            continue
        for t in range(window):
            dinic.add_edge(vid(ni, t), vid(ni, t + 1), big)

    # 2d) 每个时态焚烧端直连超级汇；该边流量 = 第 t 分钟实际送达量
    sink_edge_records = []
    for t in range(n_layers):
        fwd_idx = len(dinic.g[vid(sink_idx, t)])
        dinic.add_edge(vid(sink_idx, t), super_sink, big)
        sink_edge_records.append({"t": t, "edge_pos": fwd_idx, "capacity": big})

    # 3) 最大流 = 截止时刻前实际送达总量
    delivered = dinic.max_flow(super_source, super_sink)
    target_total = required_flow * window
    passed = delivered + EPS >= target_total

    def _flow_on(from_v: int, edge_pos: int, initial_cap: float) -> float:
        """由前向边残余容量反推该边实际承担的流量。"""
        fwd = dinic.g[from_v][edge_pos]
        return initial_cap - fwd.cap

    # 逐分钟送达量（sink 收集边流量），按到达分钟
    deliveries = []
    for rec in sink_edge_records:
        f = _flow_on(vid(sink_idx, rec["t"]), rec["edge_pos"], rec["capacity"])
        if f > EPS:
            deliveries.append(
                {
                    "minute": rec["t"],
                    "delivered": _num(f),
                }
            )

    # 逐分钟源头注入量（审计通过但窗口不足时可用于对比“已产生但滞留”的量）
    supplies = []
    for rec in supply_records:
        f = _flow_on(super_source, rec["edge_pos"], rec["capacity"])
        supplies.append({"minute": rec["t"], "produced": _num(f)})

    # 管段—出发分钟流量（仅列出实际有流量的实例，避免大量零行）
    edge_flows = []
    per_edge_totals: dict[int, float] = {}
    for rec in pipe_records:
        f = _flow_on(rec["from_vid"], rec["edge_pos"], rec["capacity"])
        per_edge_totals[rec["edge_index"]] = per_edge_totals.get(rec["edge_index"], 0.0) + f
        if f > EPS:
            edge_flows.append(
                {
                    "edge_index": rec["edge_index"],
                    "position": rec["position"],
                    "edge_id": rec["id"],
                    "from": rec["from"],
                    "to": rec["to"],
                    "duration": rec["duration"],
                    "departure_minute": rec["departure_minute"],
                    "arrival_minute": rec["arrival_minute"],
                    "flow": _num(f),
                    "capacity": _num(rec["capacity"]),
                }
            )

    # 管段在整个窗口内承担的合计流量（快速定位热点管段）
    edge_totals = [
        {
            "edge_index": e["index"],
            "position": e["position"],
            "edge_id": e["id"],
            "from": e["from"],
            "to": e["to"],
            "duration": e["duration"],
            "total_flow": _num(per_edge_totals.get(e["index"], 0.0)),
        }
        for e in clean_edges
    ]

    result = {
        "passed": passed,
        "stage": "time_window",
        "window_minutes": window,
        "required_flow": _num(required_flow),
        "target_total": _num(target_total),
        "delivered_total": _num(delivered),
        "shortage": _num(max(0.0, target_total - delivered)),
        "audit": {
            "passed": True,
            "normal_max_flow": audit["normal"]["max_flow"],
        },
        "deliveries": deliveries,
        "production": supplies,
        "edge_flows": edge_flows,
        "edge_totals": edge_totals,
        "failure": None,
    }

    # 4) 未通过：按时间层最小割给出可复核证据
    if not passed:
        result["failure"] = _time_window_cut(
            dinic=dinic,
            super_source=super_source,
            n_layers=n_layers,
            all_nodes=all_nodes,
            index_of=index_of,
            source=source,
            sink=sink,
            window=window,
            clean_edges=clean_edges,
            vid=vid,
            pipe_records=pipe_records,
            supply_records=supply_records,
            delivered=delivered,
            target_total=target_total,
            required_flow=required_flow,
        )

    return result


def _time_node_label(name: str, t: int) -> str:
    """时态节点的稳定标签，如 ``汇合点A@t=3``。"""
    return f"{name}@t={t}"


def _time_window_cut(
    *,
    dinic: Dinic,
    super_source: int,
    n_layers: int,
    all_nodes: list[str],
    index_of: dict,
    source: str,
    sink: str,
    window: int,
    clean_edges: list[dict],
    vid,
    pipe_records: list[dict],
    supply_records: list[dict],
    delivered: float,
    target_total: float,
    required_flow: float,
) -> dict:
    """由时间层残余网络构造可复核的最小割证据。"""
    side = dinic.reachable_from_source(super_source)

    source_side = []
    sink_side = []
    # 每个时态节点 (node, t) 是否落在超级源可达（源侧）集合，t = 0 … W
    reachable: dict[tuple[int, int], bool] = {}
    for ni, name in enumerate(all_nodes):
        for t in range(n_layers):
            reachable[(t, ni)] = side[vid(ni, t)]
            label = _time_node_label(name, t)
            (source_side if side[vid(ni, t)] else sink_side).append(
                {"node": name, "minute": t, "label": label}
            )
    source_side.sort(key=lambda x: (x["minute"], x["node"]))
    sink_side.sort(key=lambda x: (x["minute"], x["node"]))

    # 由前向边残余容量反推实际流量（建边时已记录邻接表位置，不能按
    # “第几条出边”推算——残量反向边会与前向边交错）。
    def _edge_flow(from_v: int, pos: int, cap: float) -> float:
        fwd = dinic.g[from_v][pos]
        return cap - fwd.cap

    supply_flows = {
        rec["t"]: _edge_flow(super_source, rec["edge_pos"], rec["capacity"])
        for rec in supply_records
    }

    # 跨层割管段：实例边 (u,t)→(v,t+d) 起点在源侧、终点在焚烧端侧者。
    # 最小割中的这类边必被最大流饱和（否则终点沿残余边仍可达），是卡住
    # 输送的跨层容量瓶颈。
    cut_edges = []
    cut_capacity = 0.0
    bottleneck_minutes = []
    for rec in pipe_records:
        t = rec["departure_minute"]
        u = index_of[rec["from"]]
        v = index_of[rec["to"]]
        if reachable[(t, u)] and not reachable[(t + rec["duration"], v)]:
            flow = _edge_flow(rec["from_vid"], rec["edge_pos"], rec["capacity"])
            cut_edges.append(
                {
                    "kind": "pipe",
                    "edge_index": rec["edge_index"],
                    "position": rec["position"],
                    "edge_id": rec["id"],
                    "from": rec["from"],
                    "to": rec["to"],
                    "duration": rec["duration"],
                    "departure_minute": t,
                    "arrival_minute": rec["arrival_minute"],
                    "from_time_node": _time_node_label(rec["from"], t),
                    "to_time_node": _time_node_label(rec["to"], rec["arrival_minute"]),
                    "capacity": _num(rec["capacity"]),
                    "flow": _num(flow),
                    "saturated": flow + EPS >= rec["capacity"],
                }
            )
            cut_capacity += rec["capacity"]
            bottleneck_minutes.append(t)
    cut_edges.sort(key=lambda x: (x["departure_minute"], x["position"]))

    # 供给边 super_source→(source,t) 若跨割（(source,t) 在焚烧端侧），
    # 则在最大流中必被打满，其流量即已按时入网并最终送达的部分。
    # 跨割供给边 + 跨层管段共同构成时间层最小割。
    cut_supplies = []
    for rec in supply_records:
        t = rec["t"]
        if not reachable[(t, index_of[source])]:
            cut_supplies.append(
                {
                    "kind": "supply",
                    "minute": t,
                    "time_node": _time_node_label(source, t),
                    "capacity": _num(rec["capacity"]),
                    "flow": _num(supply_flows[t]),
                }
            )
            cut_capacity += rec["capacity"]
    cut_supplies.sort(key=lambda x: x["minute"])

    # 等待边 / 焚烧端收集边容量为 big（大于应排总量），不可能跨越最小割，
    # 故割容量只由供给边与管段实例边构成，且依定理恰等于实际送达总量。

    # “气体滞留途中被误判安全”的直接信号：源头该分钟注入未打满。
    stranded_minutes = sorted(
        t for t, f in supply_flows.items() if f + EPS < required_flow
    )
    # 首个受限时刻：最早“注入不足（滞留）分钟”与最早“饱和跨层瓶颈
    # 出发分钟”中更早者。
    constrained = stranded_minutes + bottleneck_minutes
    first_constrained_minute = min(constrained) if constrained else 0

    return {
        "stage": "time_window",
        "reason": "窗口内时间层最大送达量低于源头应排总量（详见时间层最小割）",
        "first_constrained_minute": first_constrained_minute,
        "stranded_from_minute": stranded_minutes[0] if stranded_minutes else None,
        "target_total": _num(target_total),
        "delivered_total": _num(delivered),
        "shortage": _num(max(0.0, target_total - delivered)),
        "cut": {
            "capacity": _num(cut_capacity),
            "delivered_total": _num(delivered),
            "source_side_time_nodes": source_side,
            "sink_side_time_nodes": sink_side,
            "cut_supply_edges": cut_supplies,
            "cut_pipe_edges": cut_edges,
        },
    }
