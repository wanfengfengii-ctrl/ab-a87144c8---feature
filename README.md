# 化工园区事故导排网络 · 检修前审计与限时送达复核

安全工程师在检修事故导排总管前，录入：

- **一个泄压源**、**一个安全焚烧端**、若干**汇合节点**；
- 若干带**方向**、**最大流量（容量上限）**、**正整数输送时长（分钟）**、
  **可检修标记**的管段；
- 事故时必须持续排出的流量。

系统先在**正常网络**与**每一条可检修管段单独临时失效后的残余网络**上，
分别独立运行最大流（Dinic），给出每个情景的**最大可导排量**。
所有情景均不低于事故要求流量才放行；失败时按管段**录入顺序**
返回首条不达标管段，并依据最大流 / 最小割定理给出可复核的
**源侧割集节点、焚烧端侧节点、割集管段与割集容量**。

审计通过后可再做**限时送达复核**：服务端按草稿重新审计，随后把每分钟
展开为有向时间层（管段容量按分钟生效、气体可在汇合节点等待、超窗到达
不计入），以源头逐分钟持续产生的总量为目标，返回截止时刻前**实际送达
量**与**管段—时段流量**；仅当全部应排量按时到达才通过，否则按时间层
最小割给出首个受限时刻、源侧 / 焚烧端侧时态节点与跨层管段容量。

> 结论以**流量**为准而非路径条数：存在多条路径不代表总排量达标，
> 共享瓶颈会限制总流量（见 `tests/test_flow.py::test_shared_bottleneck_not_path_count`）。

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
割集容量 == 最大流、非法节点引用返回 400；以及限时复核的按时放行、
超窗滞留拦截（时间层割容量 == 实际送达量）、审计失败短路、
输送时长非正整数 400。

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

检修审计通过后，安全工程师为每条管段补录**正整数输送时长**（分钟），
并填写事故最初若干分钟（窗口）内必须送达焚烧端的**持续泄压流量**
（复用 `required_flow`）与**窗口长度** `window_minutes`。

服务端**先按既有规则重新审计当前草稿**：审计不通过直接返回
`stage="audit_failed"`，不展开时间层；审计通过后再把每分钟展开为一个
有向时间层：

- 源头在 `t = 0 … W-1` 每分钟持续产生 `required_flow`，目标总量
  `required_flow × W`；
- 管段 `u→v`（时长 `d`、容量 `c`）对每个可行出发分钟 `t`（`t+d ≤ W`）
  展开为 `(u,t)→(v,t+d)`，容量 `c` **按分钟生效**（每个实例独立占满
  完整容量）；`t+d > W` 的实例不建边——**超窗到达不计入**；
- 非焚烧端节点有无限容量等待边 `(v,t)→(v,t+1)`，气体**可在汇合节点
  等待**；焚烧端不设等待边，到达即计入送达；
- 共 `W+1` 个时间层（`0…W`），截止时刻 `t=W` 恰好到达仍计入。

请求体（在审计请求体上增加 `window_minutes`，每条管段增加 `duration`）：

```json
{
  "source": "泄压源V-101", "sink": "焚烧炉F-1",
  "required_flow": 50, "window_minutes": 5,
  "nodes": [],
  "edges": [
    {"id": "E1", "from": "泄压源V-101", "to": "焚烧炉F-1",
     "capacity": 100, "maintainable": false, "duration": 2}
  ]
}
```

上例中仅 `t=0…3` 的产出能在 `t+2 ≤ 5` 到达（共 200），第 4 分钟产出的
50 单位必然超窗，故不通过。响应（节选）：

```json
{
  "passed": false, "stage": "time_window",
  "window_minutes": 5, "required_flow": 50,
  "target_total": 250, "delivered_total": 200, "shortage": 50,
  "audit": {"passed": true, "normal_max_flow": 100},
  "deliveries": [
    {"minute": 2, "delivered": 50}, {"minute": 3, "delivered": 50},
    {"minute": 4, "delivered": 50}, {"minute": 5, "delivered": 50}],
  "production": [
    {"minute": 0, "produced": 50}, {"minute": 1, "produced": 50},
    {"minute": 2, "produced": 50}, {"minute": 3, "produced": 50},
    {"minute": 4, "produced": 0}],
  "edge_flows": [
    {"edge_id": "E1", "departure_minute": 0, "arrival_minute": 2,
     "flow": 50, "capacity": 100, "duration": 2}
  ],
  "failure": {
    "first_constrained_minute": 4,
    "stranded_from_minute": 4,
    "cut": {
      "capacity": 200, "delivered_total": 200,
      "source_side_time_nodes": [
        {"node": "泄压源V-101", "minute": 4, "label": "泄压源V-101@t=4"},
        {"node": "泄压源V-101", "minute": 5, "label": "泄压源V-101@t=5"}],
      "sink_side_time_nodes": [
        {"node": "泄压源V-101", "minute": 0, "label": "泄压源V-101@t=0"}],
      "cut_supply_edges": [
        {"minute": 0, "time_node": "泄压源V-101@t=0", "capacity": 50, "flow": 50},
        {"minute": 1, "time_node": "泄压源V-101@t=1", "capacity": 50, "flow": 50},
        {"minute": 2, "time_node": "泄压源V-101@t=2", "capacity": 50, "flow": 50},
        {"minute": 3, "time_node": "泄压源V-101@t=3", "capacity": 50, "flow": 50}],
      "cut_pipe_edges": []
    }
  }
}
```

当限制来自按分钟生效的容量瓶颈（而非纯延迟）时，`cut_pipe_edges` 会
列出饱和的跨层管段实例，例如：

```json
"cut_pipe_edges": [
  {"edge_id": "E3", "from_time_node": "S@t=2", "to_time_node": "T@t=3",
   "departure_minute": 2, "arrival_minute": 3,
   "capacity": 30, "flow": 30, "saturated": true}
]
```

最小割可独立复核：**跨割源头供给边容量 + 跨层管段容量之和恰为
`cut.capacity`，依最大流 / 最小割定理等于 `delivered_total`**。
`first_constrained_minute` 取“最早源头注入不足分钟（气体滞留）”与
“最早饱和跨层割管段出发分钟（按分钟生效的容量瓶颈）”中更早者，
配合源侧 / 焚烧端侧时态节点（标签 `节点@t=分钟`）即可定位延迟或瓶颈。

`duration` 不是正整数、`window_minutes` 不是正整数等返回 `HTTP 400`。

### `GET /health`

容器健康检查端点，返回 `{"status":"ok",...}`。

## 前端交互约定

- 页面分四区：**当前输入（草稿）**、**输入被拒绝（400）**、**审计结论**、
  **限时送达复核**。
- 提交审计后通过真实业务 API 渲染正常网络与逐条失效情形的最大可导排量。
- 通过时显示放行结论；失败时高亮首条失效管段并展示最小割证据。
- 草稿在上次审计之后被任何修改时，旧审计结论区顶部出现过期警示，
  旧结论不会被当作新草稿的结果；重新审计后才刷新。
- 管段表含每条管段的正整数**输送时长**；限时窗口区填写窗口长度后经
  真实业务 API `POST /api/time-window-review` 重新计算。
- **管网草稿或窗口在上次限时复核之后被任何修改（含新增/删除管段、
  改时长/容量/节点）时，旧限时结论立即标明过期**，重新复核后才刷新；
  限时结论与审计结论各自独立标记过期，互不冒用。

## 项目结构

```
app/
  flow.py            # Dinic 最大流 + 残余网络最小割 + 审计编排 +
                     #   时间展开网络限时复核（实际送达量 / 时间层最小割）
  main.py            # FastAPI：/api/audit、/api/time-window-review、/health、静态页面
  static/            # 原生前端（无构建步骤）
tests/               # pytest：引擎/审计/时间层逻辑 + API
scripts/verify       # 一次性校验：测试 + 构建 + API 冒烟（退出码报告）
Dockerfile
docker-compose.yml   # web（常驻，健康检查，端口可配）+ verify（一次性）
```
