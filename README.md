# 化工园区事故导排网络 · 检修前审计与限时窗口复核

安全工程师在检修事故导排总管前，录入：

- **一个泄压源**、**一个安全焚烧端**、若干**汇合节点**；
- 若干带**方向**、**最大流量（容量上限）**、**正整数输送时长（分钟）**、
  **可检修标记**的管段；
- 事故时必须持续排出的流量。

系统分两步给出结论：

1. **检修前审计**：在**正常网络**与**每一条可检修管段单独临时失效后的残余
   网络**上，分别独立运行最大流（Dinic），给出每个情景的**最大可导排量**。
   所有情景均不低于事故要求流量才放行；失败时按管段**录入顺序**返回首条
   不达标管段，并依据最大流 / 最小割定理给出可复核的**源侧割集节点、
   焚烧端侧节点、割集管段与割集容量**。
2. **事故初限时窗口复核**（审计通过后）：录入事故最初每分钟源头**持续泄压
   流量**与**窗口长度（分钟）**。服务端先按既有规则**重新审计当前草稿**，
   再把每分钟展开为有向时间层——管段容量按分钟各一份、气体可在汇合节点
   等待、**到达时间超出窗口的流量不计入**；返回截止时刻前**实际按时送达量**
   与**管段—时段流量**，仅当源头逐分钟持续产生的总量全部按时到达焚烧端才
   通过。窗口不够时，按时间层最小割返回可复核的**首个受限时刻、源侧与
   焚烧端侧时态节点、跨层管段容量**，另附“受限时刻之后才到达的在途管段”
   与“到达必超窗口、按规则未展开的管段时段”，用于定位延迟或瓶颈。

> 结论以**流量**为准而非路径条数：存在多条路径不代表总排量达标，
> 共享瓶颈会限制总流量（见 `tests/test_flow.py::test_shared_bottleneck_not_path_count`）。
> 限时复核中，**滞留在管段内、截止时刻仍未抵达焚烧端的气体同样不计入安全
> 送达**（见 `tests/test_time_window.py::test_pure_delay_nothing_arrives_in_window`）。

## 快速开始（Docker Compose）

```bash
# 构建并启动常驻 Web 服务（默认宿主机端口 8080，可配置）
docker compose up -d --build web
# 浏览器打开 http://localhost:8080

# 自定义宿主机端口
WEB_HOST_PORT=9090 docker compose up -d web
# 或复制 .env.example 为 .env 后修改 WEB_HOST_PORT
```

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok","service":"flare-audit", ...}
```

## 一次性交付校验服务 verify

`verify` 是一次性服务：依次运行 **pytest 代码测试 → 构建检查
（字节码编译 + 应用导入）→ 真实拉起 uvicorn 的导排 API 冒烟**，
随后自行退出，**退出码即结论**（0 全部通过，非 0 存在失败项）：

```bash
docker compose build verify
docker compose run --rm verify
echo "exit code = $?"
```

冒烟覆盖：健康检查、达标网络放行、失效网络返回首条失效管段且
割集容量 == 最大流、限时窗口按时送达、限时不足返回首个受限时刻与
时间层最小割（割容量 == 累计实到）、非法节点引用 / 缺少输送时长返回 400。

## 本地开发（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

pytest -q                      # 代码测试
python scripts/verify          # 与容器内一致的一次性校验
uvicorn app.main:app --reload  # 开发服务
```

## 业务 API

### `POST /api/audit`

请求体：

```json
{
  "source": "泄压源V-101",
  "sink": "焚烧炉F-1",
  "required_flow": 95,
  "nodes": ["汇合点A", "汇合点B"],
  "edges": [
    {"id": "E1", "from": "泄压源V-101", "to": "汇合点A", "capacity": 100, "maintainable": true}
  ]
}
```

- `nodes` 仅列汇合节点；泄压源与安全焚烧端自动并入节点集合。
- `capacity` 为管段容量上限，必须为正数；方向为 `from → to`，不可逆向。
- 仅 `maintainable: true` 的管段参与“单管段临时失效”模拟。

响应（失败时节选）：

```json
{
  "passed": false,
  "required_flow": 95,
  "normal": {"max_flow": 100, "meets": true, "cut": { ... }},
  "scenarios": [
    {"position": 1, "edge_id": "E1", "from": "...", "to": "...",
     "capacity": 100, "max_flow": 90, "meets": false}
  ],
  "failure": {
    "stage": "single_failure",
    "position": 1,
    "edge_id": "E1",
    "max_flow": 90,
    "required_flow": 95,
    "cut": {
      "capacity": 90,
      "source_side_nodes": ["泄压源V-101", "汇合点B"],
      "sink_side_nodes": ["汇合点A", "焚烧炉F-1"],
      "cut_edges": [ {"position": 3, "id": "E3", "from": "...", "to": "...", "capacity": 90} ]
    }
  }
}
```

割集可独立复核：把节点按 `source_side_nodes / sink_side_nodes` 两分组，
所有从源侧指向焚烧端侧的管段容量之和应恰为 `capacity`，且依据
最大流 / 最小割定理等于该情景最大可导排量。

输入无效（方向自环、容量非正数、节点引用不存在、源汇相同、必填为空等）
返回 `HTTP 400`：

```json
{"error": "第 1 条管段终点“X”未在节点中定义", "field": "edges[0].to"}
```

### `POST /api/time-window-review`

检修前审计通过后的事故初限时窗口复核。请求体在 `/api/audit` 基础上增加：

- `relief_flow`：事故最初源头**每分钟持续泄压流量**（正数）；
- `window_minutes`：**限时窗口长度**（正整数分钟）；
- 每条 `edges[].transit_minutes`：该管段的**正整数输送时长**（分钟）。

```json
{
  "source": "泄压源V-101",
  "sink": "焚烧炉F-1",
  "required_flow": 95,
  "relief_flow": 100,
  "window_minutes": 30,
  "nodes": ["汇合点A"],
  "edges": [
    {"id": "E1", "from": "泄压源V-101", "to": "汇合点A",
     "capacity": 100, "maintainable": true, "transit_minutes": 2},
    {"id": "E2", "from": "汇合点A", "to": "焚烧炉F-1",
     "capacity": 100, "maintainable": true, "transit_minutes": 1}
  ]
}
```

时间层语义：第 `s` 分钟（层 `s = 0,…,W-1`）源头注入 `relief_flow`；管段
`(u→v, d)` 在每个出发层 `s` 复制一份容量为管段容量的跨层边
`(u,s)→(v,s+d)`，**容量按分钟生效**；仅汇合节点可跨层等待；`s+d > W`
的副本不展开（超窗到达不计入）。

响应（通过时节选）：

```json
{
  "audit_passed": true,
  "window_passed": true,
  "review_stage": "window",
  "relief_flow": 100,
  "window_minutes": 30,
  "total_target_flow": 3000,
  "delivered_flow": 3000,
  "shortfall_flow": 0,
  "generated_by_minute": [{"minute": 0, "flow": 100, "target_flow": 100}],
  "delivered_by_minute": [{"minute": 1, "flow": 100}],
  "edge_periods": [
    {"edge_id": "E1", "transit_minutes": 2, "total_flow": 600,
     "periods": [
       {"departure_minute": 0, "arrival_minute": 2, "capacity": 100, "flow": 100}
     ]}
  ],
  "bottleneck": null
}
```

窗口不够时 `window_passed=false`，`bottleneck` 给出**首个受限时刻**的
时间层最小割，可独立复核：

```json
{
  "bottleneck": {
    "first_restricted_minute": 1,
    "cumulative_required_flow": 100,
    "cumulative_delivered_flow": 60,
    "deficit_flow": 40,
    "cut": {
      "capacity": 60,
      "source_side_temporal_nodes": [{"node": "泄压源V-101", "minute": 0}],
      "sink_side_temporal_nodes": [
        {"node": "泄压源V-101", "minute": 1},
        {"node": "焚烧炉F-1", "minute": 0},
        {"node": "焚烧炉F-1", "minute": 1}
      ],
      "cross_layer_edges": [
        {"edge_id": "E1", "transit_minutes": 1,
         "departure_minute": 0, "arrival_minute": 1,
         "capacity": 60, "flow": 60,
         "from_temporal": {"node": "泄压源V-101", "minute": 0},
         "to_temporal": {"node": "焚烧炉F-1", "minute": 1}}
      ],
      "cross_injection_edges": []
    },
    "late_arriving_edges": [],
    "out_of_window_edges": []
  }
}
```

- `cut.capacity` 依最大流 / 最小割定理**恰等于该前缀累计按时送达量**；
  将 `source_side_temporal_nodes / sink_side_temporal_nodes` 二分，所有
  从源侧时态节点跨到焚烧端侧时态节点的 `cross_layer_edges` 容量与
  `cross_injection_edges`（该分钟产量未能入网）容量之和应恰为 `capacity`。
- `late_arriving_edges`：已承载流量、但在首个受限时刻**之后**才到达的
  在途跨层管段（不计入该刻送达，定位延迟）。
- `out_of_window_edges`：输送时长使其**到达必超窗口**、按规则未展开的
  管段时段（`flow` 为 `null`，定位纯延迟瓶颈）。

若当前草稿**重新审计不通过**，则不展开时间层：
`audit_passed=false, review_stage="audit", audit={...完整审计结论...}`。
输送时长缺失 / 非正整数、窗口长度非正整数、泄压流量非正数等返回
`HTTP 400`（错误信息含对应 `field`）。

### `GET /health`

容器健康检查端点，返回 `{"status":"ok",...}`。

## 前端交互约定

- 页面分区：**当前输入（草稿）**、**限时窗口参数**、**输入被拒绝（400）**、
  **审计结论**、**限时窗口复核结论**。
- 管段表每行除方向 / 容量 / 可检修外，还须填**正整数输送时长（分钟）**。
- 提交审计后通过真实业务 API 渲染正常网络与逐条失效情形的最大可导排量。
- 通过时显示放行结论；失败时高亮首条失效管段并展示最小割证据。
- 草稿在上次审计之后被任何修改时，旧审计结论区顶部出现过期警示；
  管网草稿或窗口参数在上次限时复核之后被任何修改时，旧**窗口结论**同样
  立即标记过期（两条过期链互不干扰）；旧结论不会被当作新输入的结果，
  重新提交并通过真实业务 API 计算后才刷新。
- 限时复核结论展示：应排 / 实际按时送达 / 缺口、逐分钟产生 vs 送达、
  管段—时段流量表；不足时展示首个受限时刻的时间层最小割与时态节点二分。

## 项目结构

```
app/
  flow.py            # Dinic 最大流 + 残余网络最小割 + 审计编排；
                     # 有向时间层展开（超级源/超级汇、逐层激活）与限时复核
  main.py            # FastAPI：/api/audit、/api/time-window-review、/health、静态页面
  static/            # 原生前端（无构建步骤）
tests/               # pytest：引擎/审计 + 时间层复核 + API
scripts/verify       # 一次性校验：测试 + 构建 + 双 API 冒烟（退出码报告）
Dockerfile
docker-compose.yml   # web（常驻，健康检查，端口可配）+ verify（一次性）
```
