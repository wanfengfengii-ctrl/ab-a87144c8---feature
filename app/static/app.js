/* 事故导排网络检修审计 —— 前端逻辑
 *
 * 职责边界：前端只负责录入草稿与展示**服务端业务 API** 返回的结论，
 * 不在本地计算任何最大流 / 判定。草稿一旦在上次审计后被改动，旧结论
 * 立即标记为过期，不会被当作当前草稿的结果。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    nodes: [],          // 汇合节点名称（不含源/汇）
    edges: [],          // {id, from, to, capacity, transit_minutes, maintainable}
    lastAuditSignature: null,  // 上次成功提交审计时草稿的签名
    lastWindowSignature: null, // 上次成功限时复核时（草稿+窗口参数）的签名
  };

  /* ---------------- 示例数据 ---------------- */

  // 达标示例：两条 100 容量干线在源/汇之间并联，要求 95，
  // 任一可检修管段失效后仍至少剩 100 容量。
  const EXAMPLE_PASS = {
    source: "泄压源V-101",
    sink: "焚烧炉F-1",
    required_flow: 95,
    nodes: ["汇合点A", "汇合点B"],
    edges: [
      { id: "E1", from: "泄压源V-101", to: "汇合点A", capacity: 100, transit_minutes: 1, maintainable: true },
      { id: "E2", from: "汇合点A", to: "焚烧炉F-1", capacity: 100, transit_minutes: 1, maintainable: true },
      { id: "E3", from: "泄压源V-101", to: "汇合点B", capacity: 100, transit_minutes: 1, maintainable: true },
      { id: "E4", from: "汇合点B", to: "焚烧炉F-1", capacity: 100, transit_minutes: 1, maintainable: true },
      { id: "E5", from: "汇合点A", to: "汇合点B", capacity: 40, transit_minutes: 2, maintainable: false },
    ],
  };

  // 失效示例：正常网络最大流 100，但首条可检修管段 E1 失效后
  // 上干线路径中断，仅剩 90，低于要求 95。
  const EXAMPLE_FAIL = {
    source: "泄压源V-101",
    sink: "焚烧炉F-1",
    required_flow: 95,
    nodes: ["汇合点A", "汇合点B"],
    edges: [
      { id: "E1", from: "泄压源V-101", to: "汇合点A", capacity: 100, transit_minutes: 1, maintainable: true },
      { id: "E2", from: "汇合点A", to: "焚烧炉F-1", capacity: 100, transit_minutes: 1, maintainable: true },
      { id: "E3", from: "泄压源V-101", to: "汇合点B", capacity: 90, transit_minutes: 2, maintainable: true },
      { id: "E4", from: "汇合点B", to: "焚烧炉F-1", capacity: 90, transit_minutes: 1, maintainable: true },
    ],
  };

  /* ---------------- 草稿渲染 ---------------- */

  function renderNodes() {
    const box = $("nodes-list");
    box.innerHTML = "";
    if (state.nodes.length === 0) {
      box.innerHTML = '<span class="tip">尚无汇合节点，点击右上方按钮添加。</span>';
      return;
    }
    state.nodes.forEach((name, i) => {
      const chip = document.createElement("span");
      chip.className = "node-chip";
      const input = document.createElement("input");
      input.type = "text";
      input.value = name;
      input.placeholder = "节点名称";
      input.addEventListener("input", () => { state.nodes[i] = input.value; markDirty(); });
      const del = document.createElement("button");
      del.type = "button";
      del.textContent = "×";
      del.title = "删除该汇合节点";
      del.addEventListener("click", () => { state.nodes.splice(i, 1); renderAll(); markDirty(); });
      chip.append(input, del);
      box.appendChild(chip);
    });
  }

  function renderEdges() {
    const body = $("edges-body");
    body.innerHTML = "";
    state.edges.forEach((edge, i) => {
      const tr = document.createElement("tr");

      const tdNo = document.createElement("td");
      tdNo.textContent = String(i + 1);

      const tdId = document.createElement("td");
      const idInput = document.createElement("input");
      idInput.type = "text";
      idInput.value = edge.id || "";
      idInput.placeholder = "选填";
      idInput.addEventListener("input", () => { edge.id = idInput.value; markDirty(); });
      tdId.appendChild(idInput);

      const tdFrom = document.createElement("td");
      const fromInput = document.createElement("input");
      fromInput.type = "text";
      fromInput.value = edge.from;
      fromInput.placeholder = "起点节点";
      fromInput.addEventListener("input", () => { edge.from = fromInput.value; markDirty(); });
      tdFrom.appendChild(fromInput);

      const tdArrow = document.createElement("td");
      tdArrow.className = "arrow";
      tdArrow.textContent = "→";

      const tdTo = document.createElement("td");
      const toInput = document.createElement("input");
      toInput.type = "text";
      toInput.value = edge.to;
      toInput.placeholder = "终点节点";
      toInput.addEventListener("input", () => { edge.to = toInput.value; markDirty(); });
      tdTo.appendChild(toInput);

      const tdCap = document.createElement("td");
      const capInput = document.createElement("input");
      capInput.type = "number";
      capInput.min = "0";
      capInput.step = "any";
      capInput.value = edge.capacity;
      capInput.addEventListener("input", () => { edge.capacity = capInput.value; markDirty(); });
      tdCap.appendChild(capInput);

      const tdTransit = document.createElement("td");
      const transitInput = document.createElement("input");
      transitInput.type = "number";
      transitInput.min = "1";
      transitInput.step = "1";
      transitInput.value = edge.transit_minutes === undefined || edge.transit_minutes === null ? "" : edge.transit_minutes;
      transitInput.placeholder = "正整数";
      transitInput.title = "气体通过该管段所需的正整数分钟数（限时窗口复核使用）";
      transitInput.addEventListener("input", () => { edge.transit_minutes = transitInput.value; markDirty(); });
      tdTransit.appendChild(transitInput);

      const tdMaint = document.createElement("td");
      tdMaint.className = "center";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!edge.maintainable;
      cb.title = "勾选后参与“单管段临时失效”模拟";
      cb.addEventListener("change", () => { edge.maintainable = cb.checked; markDirty(); });
      tdMaint.appendChild(cb);

      const tdDel = document.createElement("td");
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "row-del";
      delBtn.textContent = "×";
      delBtn.title = "删除该管段";
      delBtn.addEventListener("click", () => { state.edges.splice(i, 1); renderEdges(); markDirty(); });
      tdDel.appendChild(delBtn);

      tr.append(tdNo, tdId, tdFrom, tdArrow, tdTo, tdCap, tdTransit, tdMaint, tdDel);
      body.appendChild(tr);
    });
  }

  function renderAll() {
    renderNodes();
    renderEdges();
  }

  /* ---------------- 草稿状态 / 旧结论过期 ---------------- */

  function currentPayload() {
    const required = $("in-required").value;
    return {
      source: $("in-source").value.trim(),
      sink: $("in-sink").value.trim(),
      required_flow: required === "" ? null : Number(required),
      nodes: state.nodes.map((n) => n.trim()).filter((n) => n.length),
      edges: state.edges.map((e) => ({
        id: (e.id || "").trim() || null,
        from: (e.from || "").trim(),
        to: (e.to || "").trim(),
        capacity: e.capacity === "" || e.capacity === null ? null : Number(e.capacity),
        transit_minutes: (e.transit_minutes === "" || e.transit_minutes === undefined || e.transit_minutes === null)
          ? null : Number(e.transit_minutes),
        maintainable: !!e.maintainable,
      })),
    };
  }

  // 限时复核在草稿基础上追加泄压流量与窗口长度
  function currentWindowPayload() {
    const base = currentPayload();
    const relief = $("in-relief").value;
    const win = $("in-window").value;
    base.relief_flow = relief === "" ? null : Number(relief);
    base.window_minutes = win === "" ? null : Number(win);
    return base;
  }

  // 用稳定签名判断“草稿/窗口输入是否在上次提交后变化”
  function signature() {
    return JSON.stringify(currentPayload());
  }
  function windowSignature() {
    return JSON.stringify(currentWindowPayload());
  }

  function markDirty() {
    if (state.lastAuditSignature !== null) {
      const stale = signature() !== state.lastAuditSignature;
      $("stale-banner").classList.toggle("hidden", !stale);
      $("draft-hint").textContent = stale
        ? "草稿已修改，结论区显示的是旧结论，请重新提交审计。"
        : "";
    }
    if (state.lastWindowSignature !== null) {
      // 既有管网任何变更（含输送时长）都立即使旧窗口结论过期
      const stale = windowSignature() !== state.lastWindowSignature;
      $("window-stale-banner").classList.toggle("hidden", !stale);
      $("window-hint").textContent = stale
        ? "输入已修改，下方为旧窗口结论，请重新提交限时复核。"
        : "";
    }
  }

  function clearResult() {
    $("result-card").classList.add("hidden");
    $("reject-card").classList.add("hidden");
    $("stale-banner").classList.add("hidden");
    $("draft-hint").textContent = "";
  }

  /* ---------------- 结论渲染 ---------------- */

  function fmt(n) {
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 6 });
  }

  function edgeLabel(e) {
    const id = e.edge_id ? `（${e.edge_id}）` : "";
    return `第 ${e.position} 条管段${id}：${e.from} → ${e.to}`;
  }

  function renderChips(el, names, cls) {
    el.innerHTML = "";
    names.forEach((n) => {
      const c = document.createElement("span");
      c.className = "chip " + (cls || "");
      c.textContent = n;
      el.appendChild(c);
    });
    if (names.length === 0) {
      const c = document.createElement("span");
      c.className = "chip empty";
      c.textContent = "（无）";
      el.appendChild(c);
    }
  }

  function cutEdgeRows(cut, withSides) {
    return cut.cut_edges.map((e) => {
      const cells = withSides
        ? [e.position, e.id || "—", e.from, "→", e.to, fmt(e.capacity)]
        : [e.position, e.id || "—", `${e.from} → ${e.to}`, fmt(e.capacity)];
      return "<tr>" + cells.map((c) => `<td>${c}</td>`).join("") + "</tr>";
    }).join("");
  }

  function renderResult(data) {
    const card = $("result-card");
    card.classList.remove("hidden");
    $("reject-card").classList.add("hidden");
    $("stale-banner").classList.add("hidden");
    $("draft-hint").textContent = "";

    const passed = !!data.passed;
    $("pass-panel").classList.toggle("hidden", !passed);
    $("fail-panel").classList.toggle("hidden", passed);

    if (passed) {
      $("pass-scenario-count").textContent = String(data.scenarios.length);
      $("pass-required").textContent = fmt(data.required_flow);
    } else {
      const f = data.failure;
      const isNormal = f.stage === "normal";
      $("fail-edge").textContent = isNormal
        ? "（正常网络本身）"
        : edgeLabel(f);
      $("fail-flow").textContent = fmt(f.max_flow);
      $("fail-required").textContent = fmt(f.required_flow);

      const cut = f.cut;
      renderChips($("cut-source-side"), cut.source_side_nodes, "src");
      renderChips($("cut-sink-side"), cut.sink_side_nodes, "sink");
      $("cut-edges-body").innerHTML = cutEdgeRows(cut, true);
      $("cut-capacity").textContent = fmt(cut.capacity);
    }

    // 正常网络
    const normal = data.normal;
    $("normal-flow").textContent = fmt(normal.max_flow);
    $("normal-required").textContent = fmt(data.required_flow);
    const meetsEl = $("normal-meets");
    meetsEl.textContent = normal.meets ? "达标" : "不达标";
    meetsEl.className = "metric-value " + (normal.meets ? "meets-yes" : "meets-no");
    renderChips($("normal-cut-source"), normal.cut.source_side_nodes, "src");
    renderChips($("normal-cut-sink"), normal.cut.sink_side_nodes, "sink");
    $("normal-cut-edges").innerHTML = cutEdgeRows(normal.cut, false);
    $("normal-cut-capacity").textContent = fmt(normal.cut.capacity);

    // 逐条失效
    const body = $("scenarios-body");
    body.innerHTML = "";
    if (data.scenarios.length === 0) {
      body.innerHTML = '<tr><td colspan="7" class="center tip">没有勾选“可检修”的管段，未模拟单段失效；仅审计正常网络。</td></tr>';
    } else {
      const failIdx = data.failure && !passed ? data.failure.edge_index : null;
      data.scenarios.forEach((s) => {
        const tr = document.createElement("tr");
        if (s.edge_index === failIdx) tr.className = "row-fail";
        const cells = [
          String(s.position),
          (s.edge_id ? s.edge_id + " " : "") + `${s.from} → ${s.to}`,
          "→",
          fmt(s.capacity),
          fmt(s.max_flow),
          fmt(data.required_flow),
        ];
        cells.forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
        const tdJudge = document.createElement("td");
        const pill = document.createElement("span");
        pill.className = "pill " + (s.meets ? "yes" : "no");
        pill.textContent = s.meets ? "达标" : "不达标";
        tdJudge.appendChild(pill);
        tr.appendChild(tdJudge);
        body.appendChild(tr);
      });
    }

    const nonMaint = state.edges.filter((e) => !e.maintainable).length;
    const note = $("non-maintainable-note");
    if (nonMaint > 0) {
      note.classList.remove("hidden");
      note.textContent = `另有 ${nonMaint} 条未勾选“可检修”的管段，不参与单段临时失效模拟（视为事故期间保持投用）。`;
    } else {
      note.classList.add("hidden");
    }

    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderRejection(err) {
    $("result-card").classList.add("hidden");
    const card = $("reject-card");
    card.classList.remove("hidden");
    $("reject-msg").textContent = err || "输入无效。";
    card.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* ---------------- 提交审计（真实业务 API） ---------------- */

  async function submitAudit() {
    const payload = currentPayload();
    $("btn-audit").disabled = true;
    $("draft-hint").textContent = "正在调用服务端审计 API…";
    try {
      const resp = await fetch("/api/audit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        renderRejection(data.error || `审计请求失败（HTTP ${resp.status}）`);
        state.lastAuditSignature = null;
        return;
      }
      state.lastAuditSignature = signature();
      renderResult(data);
    } catch (e) {
      renderRejection("无法连接审计服务：" + e.message);
      state.lastAuditSignature = null;
    } finally {
      $("btn-audit").disabled = false;
      if ($("draft-hint").textContent.startsWith("正在")) $("draft-hint").textContent = "";
    }
  }

  /* ---------------- 限时窗口复核（真实业务 API） ---------------- */

  function temporalLabel(t) {
    return `(${t.node}, 第${t.minute}层)`;
  }

  function renderTemporalChips(el, nodes, cls) {
    el.innerHTML = "";
    nodes.forEach((n) => {
      const c = document.createElement("span");
      c.className = "chip " + (cls || "");
      c.textContent = temporalLabel(n);
      el.appendChild(c);
    });
    if (nodes.length === 0) {
      const c = document.createElement("span");
      c.className = "chip empty";
      c.textContent = "（无）";
      el.appendChild(c);
    }
  }

  function edgeText(e) {
    return `第${e.position}条 ${e.edge_id ? e.edge_id + " " : ""}${e.from} → ${e.to}`;
  }

  function renderWindowResult(data) {
    $("window-result").classList.remove("hidden");
    $("window-reject").classList.add("hidden");
    $("window-stale-banner").classList.add("hidden");
    $("window-hint").textContent = "";

    // 服务端先重新审计：审计不过则不展开时间层
    if (!data.audit_passed) {
      $("window-audit-block").classList.remove("hidden");
      $("window-verdict-block").classList.add("hidden");
      const f = data.audit && data.audit.failure;
      $("window-audit-reason").textContent = f
        ? (f.stage === "normal" ? "正常网络本身不达标"
          : `管段 ${edgeText(f)} 临时失效后最大可导排量 ${fmt(f.max_flow)} < 要求 ${fmt(f.required_flow)}`)
        : "审计未通过";
      $("window-result").scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }

    $("window-audit-block").classList.add("hidden");
    $("window-verdict-block").classList.remove("hidden");

    const passed = !!data.window_passed;
    $("window-pass-panel").classList.toggle("hidden", !passed);
    $("window-fail-panel").classList.toggle("hidden", passed);
    $("w-window").textContent = String(data.window_minutes);
    $("w-relief").textContent = fmt(data.relief_flow);
    $("w-delivered").textContent = fmt(data.delivered_flow);
    $("w-target").textContent = fmt(data.total_target_flow);
    $("w-delivered-f").textContent = fmt(data.delivered_flow);
    $("w-target-f").textContent = fmt(data.total_target_flow);
    $("w-shortfall").textContent = fmt(data.shortfall_flow);
    $("w-m-target").textContent = fmt(data.total_target_flow);
    $("w-m-delivered").textContent = fmt(data.delivered_flow);
    $("w-m-shortfall").textContent = fmt(data.shortfall_flow);

    // 逐分钟：产生/入网 vs 按时送达（generated s=0..W-1 对齐 delivered a=1..W）
    const body = $("w-minute-body");
    body.innerHTML = "";
    const gen = data.generated_by_minute || [];
    const arr = data.delivered_by_minute || [];
    arr.forEach((aRow, i) => {
      const gRow = gen[i] || { flow: "—" };
      const tr = document.createElement("tr");
      const td1 = document.createElement("td");
      td1.textContent = `第 ${aRow.minute} 分钟末（层 ${aRow.minute}）`;
      const td2 = document.createElement("td");
      td2.textContent = fmt(gRow.flow);
      const td3 = document.createElement("td");
      td3.textContent = fmt(aRow.flow);
      if (Number(aRow.flow) + 1e-9 < Number(gRow.flow)) td3.className = "meets-no strong";
      tr.append(td1, td2, td3);
      body.appendChild(tr);
    });

    // 管段—时段流量
    const box = $("w-edge-periods");
    box.innerHTML = "";
    (data.edge_periods || []).forEach((e) => {
      const wrap = document.createElement("div");
      wrap.className = "edge-period-block";
      const h = document.createElement("p");
      h.className = "edge-period-head";
      h.innerHTML = `${edgeText(e)}　<span class="tip">输送 ${e.transit_minutes} 分钟 · 分钟容量 ${fmt(e.capacity)} · 窗口内合计 <strong>${fmt(e.total_flow)}</strong></span>`;
      wrap.appendChild(h);
      const tbl = document.createElement("table");
      tbl.className = "cut-table";
      tbl.innerHTML = '<thead><tr><th>出发层(分钟)</th><th>到达层(分钟)</th><th>时段流量</th><th>分钟容量</th><th>使用率</th></tr></thead>';
      const tb = document.createElement("tbody");
      e.periods.forEach((p) => {
        const tr = document.createElement("tr");
        const pct = p.capacity > 0 ? Math.min(100, Math.round((p.flow / p.capacity) * 1000) / 10) : 0;
        [String(p.departure_minute), String(p.arrival_minute), fmt(p.flow), fmt(p.capacity), `${pct}%`]
          .forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
        if (p.flow > 1e-9) tr.className = "row-flow";
        tb.appendChild(tr);
      });
      tbl.appendChild(tb);
      const scroll = document.createElement("div");
      scroll.className = "table-scroll";
      scroll.appendChild(tbl);
      wrap.appendChild(scroll);
      box.appendChild(wrap);
    });

    // 瓶颈 / 时间层最小割
    const bn = data.bottleneck;
    const panel = $("w-bottleneck");
    if (!passed && bn) {
      panel.classList.remove("hidden");
      $("w-first-minute").textContent = String(bn.first_restricted_minute);
      $("w-req-at").textContent = fmt(bn.cumulative_required_flow);
      $("w-arr-at").textContent = fmt(bn.cumulative_delivered_flow);
      const cut = bn.cut;
      renderTemporalChips($("w-cut-source"), cut.source_side_temporal_nodes, "src");
      renderTemporalChips($("w-cut-sink"), cut.sink_side_temporal_nodes, "sink");

      const ce = $("w-cut-edges");
      ce.innerHTML = "";
      (cut.cross_layer_edges || []).forEach((x) => {
        const tr = document.createElement("tr");
        [
          String(x.position), edgeText(x),
          temporalLabel(x.from_temporal), "→", temporalLabel(x.to_temporal),
          `${x.transit_minutes} 分钟`, fmt(x.flow), fmt(x.capacity),
        ].forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
        ce.appendChild(tr);
      });
      (cut.cross_injection_edges || []).forEach((x) => {
        const tr = document.createElement("tr");
        [
          "—", "超级源 → 泄压源（该分钟产量未能入网）",
          "(超级源)", "→", temporalLabel(x.temporal_node),
          "—", fmt(x.flow), fmt(x.capacity),
        ].forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
        ce.appendChild(tr);
      });
      $("w-cut-capacity").textContent = fmt(cut.capacity);

      const fillEdgeRows = (tbody, rows, withArrival) => {
        tbody.innerHTML = "";
        if (!rows.length) {
          tbody.innerHTML = '<tr><td colspan="5" class="center tip">（无）</td></tr>';
          return;
        }
        rows.forEach((x) => {
          const tr = document.createElement("tr");
          const cols = withArrival
            ? [String(x.position), edgeText(x), `第${x.departure_minute}层`,
               `第${x.arrival_minute}层`, x.flow === null ? "未展开（不计入）" : fmt(x.flow)]
            : [String(x.position), edgeText(x), `第${x.departure_minute}层`,
               `第${x.arrival_minute}层`, fmt(x.flow)];
          cols.forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
          tbody.appendChild(tr);
        });
      };
      fillEdgeRows($("w-late-edges"), bn.late_arriving_edges || [], false);
      fillEdgeRows($("w-oow-edges"), bn.out_of_window_edges || [], true);
      $("w-late-block").classList.toggle("hidden", !(bn.late_arriving_edges || []).length);
      $("w-oow-block").classList.toggle("hidden", !(bn.out_of_window_edges || []).length);
    } else {
      panel.classList.add("hidden");
    }

    $("window-result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderWindowRejection(err) {
    $("window-result").classList.add("hidden");
    const el = $("window-reject");
    el.classList.remove("hidden");
    el.textContent = err || "输入无效。";
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function submitWindowReview() {
    const payload = currentWindowPayload();
    $("btn-window-review").disabled = true;
    $("window-hint").textContent = "正在调用服务端限时复核 API…";
    try {
      const resp = await fetch("/api/time-window-review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        renderWindowRejection(data.error || `限时复核请求失败（HTTP ${resp.status}）`);
        state.lastWindowSignature = null;
        return;
      }
      state.lastWindowSignature = windowSignature();
      renderWindowResult(data);
    } catch (e) {
      renderWindowRejection("无法连接限时复核服务：" + e.message);
      state.lastWindowSignature = null;
    } finally {
      $("btn-window-review").disabled = false;
      if ($("window-hint").textContent.startsWith("正在")) $("window-hint").textContent = "";
    }
  }

  /* ---------------- 载入 / 清空 ---------------- */

  function loadExample(ex) {
    clearResult();
    $("in-source").value = ex.source;
    $("in-sink").value = ex.sink;
    $("in-required").value = String(ex.required_flow);
    state.nodes = ex.nodes.slice();
    state.edges = ex.edges.map((e) => ({ ...e }));
    state.lastAuditSignature = null;
    state.lastWindowSignature = null;
    $("window-result").classList.add("hidden");
    $("window-reject").classList.add("hidden");
    $("window-stale-banner").classList.add("hidden");
    renderAll();
  }

  function clearAll() {
    clearResult();
    $("in-source").value = "";
    $("in-sink").value = "";
    $("in-required").value = "";
    $("in-relief").value = "";
    $("in-window").value = "";
    state.nodes = [];
    state.edges = [];
    state.lastAuditSignature = null;
    state.lastWindowSignature = null;
    $("window-result").classList.add("hidden");
    $("window-reject").classList.add("hidden");
    $("window-stale-banner").classList.add("hidden");
    renderAll();
  }

  /* ---------------- 事件绑定 ---------------- */

  $("btn-add-node").addEventListener("click", () => {
    state.nodes.push("");
    renderNodes();
    markDirty();
    const inputs = $("nodes-list").querySelectorAll("input");
    if (inputs.length) inputs[inputs.length - 1].focus();
  });

  $("btn-add-edge").addEventListener("click", () => {
    state.edges.push({ id: "", from: "", to: "", capacity: "", transit_minutes: "", maintainable: true });
    renderEdges();
    markDirty();
  });

  $("btn-audit").addEventListener("click", submitAudit);
  $("btn-window-review").addEventListener("click", submitWindowReview);
  $("btn-example-pass").addEventListener("click", () => loadExample(EXAMPLE_PASS));
  $("btn-example-fail").addEventListener("click", () => loadExample(EXAMPLE_FAIL));
  $("btn-clear").addEventListener("click", clearAll);
  ["in-source", "in-sink", "in-required", "in-relief", "in-window"].forEach((id) =>
    $(id).addEventListener("input", markDirty)
  );

  renderAll();
})();
