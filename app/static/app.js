/* 事故导排网络检修审计 + 限时送达复核 —— 前端逻辑
 *
 * 职责边界：前端只负责录入草稿与展示**服务端业务 API** 返回的结论，
 * 不在本地计算任何最大流 / 判定。草稿一旦在上次审计后被改动，旧结论
 * 立即标记为过期，不会被当作当前草稿的结果。检修审计与限时送达复核
 * 各自维护独立的“上次结论签名”，互不冒用结论。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const state = {
    nodes: [],          // 汇合节点名称（不含源/汇）
    edges: [],          // {id, from, to, capacity, duration, maintainable}
    lastAuditSignature: null,  // 上次成功提交审计时草稿的签名
    lastReviewSignature: null, // 上次成功提交限时复核时（草稿+窗口）的签名
  };

  /* ---------------- 示例数据 ---------------- */

  // 达标示例：两条 100 容量干线在源/汇之间并联，要求 95，
  // 任一可检修管段失效后仍至少剩 100 容量。时长均为 1~2 分钟。
  const EXAMPLE_PASS = {
    source: "泄压源V-101",
    sink: "焚烧炉F-1",
    required_flow: 95,
    nodes: ["汇合点A", "汇合点B"],
    edges: [
      { id: "E1", from: "泄压源V-101", to: "汇合点A", capacity: 100, duration: 1, maintainable: true },
      { id: "E2", from: "汇合点A", to: "焚烧炉F-1", capacity: 100, duration: 1, maintainable: true },
      { id: "E3", from: "泄压源V-101", to: "汇合点B", capacity: 100, duration: 2, maintainable: true },
      { id: "E4", from: "汇合点B", to: "焚烧炉F-1", capacity: 100, duration: 2, maintainable: true },
      { id: "E5", from: "汇合点A", to: "汇合点B", capacity: 40, duration: 1, maintainable: false },
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
      { id: "E1", from: "泄压源V-101", to: "汇合点A", capacity: 100, duration: 1, maintainable: true },
      { id: "E2", from: "汇合点A", to: "焚烧炉F-1", capacity: 100, duration: 1, maintainable: true },
      { id: "E3", from: "泄压源V-101", to: "汇合点B", capacity: 90, duration: 3, maintainable: true },
      { id: "E4", from: "汇合点B", to: "焚烧炉F-1", capacity: 90, duration: 3, maintainable: true },
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

      const tdDur = document.createElement("td");
      const durInput = document.createElement("input");
      durInput.type = "number";
      durInput.min = "1";
      durInput.step = "1";
      durInput.placeholder = "正整数";
      durInput.value = edge.duration === undefined || edge.duration === null ? "" : edge.duration;
      durInput.title = "该管段的正整数输送时长（分钟），用于限时时间层展开";
      durInput.addEventListener("input", () => { edge.duration = durInput.value; markDirty(); });
      tdDur.appendChild(durInput);

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

      tr.append(tdNo, tdId, tdFrom, tdArrow, tdTo, tdCap, tdDur, tdMaint, tdDel);
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
        // 正整数输送时长；审计接口会忽略该字段，限时复核要求其为正整数
        duration: e.duration === "" || e.duration === null ? null : Number(e.duration),
        maintainable: !!e.maintainable,
      })),
    };
  }

  // 用稳定签名判断“草稿是否在上次审计后变化”
  function signature() {
    return JSON.stringify(currentPayload());
  }

  // 限时复核签名额外包含窗口长度
  function reviewSignature() {
    return JSON.stringify({ ...currentPayload(), window_minutes: $("in-window").value });
  }

  function markDirty() {
    if (state.lastAuditSignature !== null) {
      const stale = signature() !== state.lastAuditSignature;
      $("stale-banner").classList.toggle("hidden", !stale);
      $("draft-hint").textContent = stale
        ? "草稿已修改，结论区显示的是旧结论，请重新提交审计。"
        : "";
    }
    // 管网任何变更都会使旧限时结论过期
    if (state.lastReviewSignature !== null) {
      $("tw-stale-banner").classList.toggle(
        "hidden", reviewSignature() === state.lastReviewSignature
      );
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

  /* ---------------- 限时送达复核（真实业务 API） ---------------- */

  function twEdgeLabel(e) {
    const id = e.edge_id ? `（${e.edge_id}）` : "";
    return `第 ${e.position} 条管段${id}`;
  }

  function renderTimeChips(el, items) {
    el.innerHTML = "";
    if (!items.length) {
      const c = document.createElement("span");
      c.className = "chip empty";
      c.textContent = "（无）";
      el.appendChild(c);
      return;
    }
    items.forEach((n) => {
      const c = document.createElement("span");
      // 源/汇节点用对应侧色调，其余时态节点用中性色
      c.className = "chip";
      c.textContent = n.label;
      c.title = n.node;
      el.appendChild(c);
    });
  }

  function showTwRejection(msg) {
    $("tw-result").classList.add("hidden");
    const box = $("tw-reject");
    box.classList.remove("hidden");
    box.textContent = msg;
  }

  function clearTwRejection() {
    $("tw-reject").classList.add("hidden");
    $("tw-reject").textContent = "";
  }

  function renderTwAuditFailure(audit) {
    $("tw-result").classList.remove("hidden");
    $("tw-conclusion").classList.add("hidden");
    $("tw-audit-failed").classList.remove("hidden");
    const f = audit.failure || {};
    $("tw-audit-fail-edge").textContent =
      f.stage === "normal"
        ? "（正常网络本身不达标）"
        : `第 ${f.position} 条管段${f.edge_id ? `（${f.edge_id}）` : ""}：${f.from} → ${f.to}`;
  }

  function renderTimeWindow(data) {
    clearTwRejection();
    $("tw-result").classList.remove("hidden");
    $("tw-audit-failed").classList.add("hidden");
    $("tw-conclusion").classList.remove("hidden");

    const f = data.failure;
    const passed = !!data.passed;
    $("tw-pass-panel").classList.toggle("hidden", !passed);
    $("tw-fail-panel").classList.toggle("hidden", passed);

    $("tw-m-target").textContent = fmt(data.target_total);
    $("tw-m-delivered").textContent = fmt(data.delivered_total);
    $("tw-m-shortage").textContent = fmt(data.shortage || 0);
    $("tw-target").value = `${fmt(data.required_flow)} × ${data.window_minutes} = ${fmt(data.target_total)}`;

    if (passed) {
      $("tw-pass-window").textContent = String(data.window_minutes);
      $("tw-pass-required").textContent = fmt(data.required_flow);
      $("tw-pass-target").textContent = fmt(data.target_total);
      $("tw-pass-delivered").textContent = fmt(data.delivered_total);
    } else {
      $("tw-fail-target").textContent = fmt(f.target_total);
      $("tw-fail-delivered").textContent = fmt(f.delivered_total);
      $("tw-fail-shortage").textContent = fmt(f.shortage);
      $("tw-fail-minute").textContent = String(f.first_constrained_minute);
      $("tw-fail-stranded").textContent =
        f.stranded_from_minute === null || f.stranded_from_minute === undefined
          ? "（跨层管段容量先受限）"
          : `（第 ${f.stranded_from_minute} 分钟起源头产量已无法按时入网，存在途中滞留）`;
    }

    // 逐分钟送达（补齐 0…W 中无送达的分钟，直观显示延迟）
    const dMap = new Map((data.deliveries || []).map((d) => [d.minute, d.delivered]));
    const dBody = $("tw-deliveries-body");
    dBody.innerHTML = "";
    for (let t = 0; t <= data.window_minutes; t += 1) {
      const tr = document.createElement("tr");
      const v = dMap.has(t) ? dMap.get(t) : 0;
      [String(t), fmt(v), t < data.window_minutes ? fmt(data.required_flow) : "—（截止时刻）"]
        .forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
      if (v === 0) tr.className = "row-fail";
      dBody.appendChild(tr);
    }

    // 管段—时段流量
    const fBody = $("tw-flows-body");
    fBody.innerHTML = "";
    const flows = (data.edge_flows || []).slice().sort(
      (a, b) => a.departure_minute - b.departure_minute || a.position - b.position
    );
    if (!flows.length) {
      fBody.innerHTML = '<tr><td colspan="7" class="center tip">窗口内没有任何管段承担流量（气体无法到达焚烧端）。</td></tr>';
    } else {
      flows.forEach((x) => {
        const tr = document.createElement("tr");
        [
          twEdgeLabel(x),
          `${x.from} → ${x.to}`,
          `${x.duration} 分钟`,
          String(x.departure_minute),
          String(x.arrival_minute),
          fmt(x.flow),
          fmt(x.capacity),
        ].forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
        fBody.appendChild(tr);
      });
    }

    // 时间层最小割
    const evidence = $("tw-cut-evidence");
    if (!passed && f && f.cut) {
      evidence.classList.remove("hidden");
      const cut = f.cut;
      $("tw-cut-identity").textContent =
        `割容量 ${fmt(cut.capacity)} = 实际送达 ${fmt(cut.delivered_total)}`;
      renderTimeChips($("tw-cut-source"), cut.source_side_time_nodes);
      renderTimeChips($("tw-cut-sink"), cut.sink_side_time_nodes);

      const pBody = $("tw-cut-pipes-body");
      pBody.innerHTML = "";
      if (!cut.cut_pipe_edges.length) {
        pBody.innerHTML = '<tr><td colspan="8" class="center tip">无跨层割管段：限制来自输送延迟（末尾几分钟产出无法在截止前到达），见下方跨割供给边。</td></tr>';
      } else {
        cut.cut_pipe_edges.forEach((p) => {
          const tr = document.createElement("tr");
          [
            String(p.position), twEdgeLabel(p), p.from_time_node, "→", p.to_time_node,
            fmt(p.capacity), fmt(p.flow),
          ].forEach((c) => { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); });
          const tdSat = document.createElement("td");
          tdSat.textContent = p.saturated ? "是（瓶颈）" : "否";
          tdSat.className = p.saturated ? "saturated-yes" : "saturated-no";
          tr.appendChild(tdSat);
          pBody.appendChild(tr);
        });
      }

      const sBody = $("tw-cut-supplies-body");
      sBody.innerHTML = "";
      cut.cut_supply_edges.forEach((s) => {
        const tr = document.createElement("tr");
        [String(s.minute), s.time_node, fmt(s.capacity), fmt(s.flow)].forEach((c) => {
          const td = document.createElement("td"); td.textContent = c; tr.appendChild(td);
        });
        sBody.appendChild(tr);
      });
    } else {
      evidence.classList.add("hidden");
    }

    $("tw-result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function submitTimeWindowReview() {
    const payload = currentPayload();
    const w = $("in-window").value;
    payload.window_minutes = w === "" ? null : Number(w);
    $("btn-tw-review").disabled = true;
    clearTwRejection();
    try {
      const resp = await fetch("/api/time-window-review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        state.lastReviewSignature = null;
        $("tw-stale-banner").classList.add("hidden");
        showTwRejection(data.error || `限时复核请求失败（HTTP ${resp.status}）`);
        return;
      }
      state.lastReviewSignature = reviewSignature();
      $("tw-stale-banner").classList.add("hidden");
      if (data.stage === "audit_failed") {
        renderTwAuditFailure(data.audit);
      } else {
        renderTimeWindow(data);
      }
    } catch (e) {
      state.lastReviewSignature = null;
      showTwRejection("无法连接限时复核服务：" + e.message);
    } finally {
      $("btn-tw-review").disabled = false;
    }
  }

  /* ---------------- 载入 / 清空 ---------------- */

  function loadExample(ex) {
    clearResult();
    clearTwResult();
    $("in-source").value = ex.source;
    $("in-sink").value = ex.sink;
    $("in-required").value = String(ex.required_flow);
    state.nodes = ex.nodes.slice();
    state.edges = ex.edges.map((e) => ({ ...e }));
    state.lastAuditSignature = null;
    state.lastReviewSignature = null;
    renderAll();
  }

  function clearAll() {
    clearResult();
    clearTwResult();
    $("in-source").value = "";
    $("in-sink").value = "";
    $("in-required").value = "";
    $("in-window").value = "";
    $("tw-target").value = "";
    state.nodes = [];
    state.edges = [];
    state.lastAuditSignature = null;
    state.lastReviewSignature = null;
    renderAll();
  }

  function clearTwResult() {
    $("tw-result").classList.add("hidden");
    $("tw-reject").classList.add("hidden");
    $("tw-stale-banner").classList.add("hidden");
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
    state.edges.push({ id: "", from: "", to: "", capacity: "", duration: "", maintainable: true });
    renderEdges();
    markDirty();
  });

  $("btn-audit").addEventListener("click", submitAudit);
  $("btn-tw-review").addEventListener("click", submitTimeWindowReview);
  $("btn-example-pass").addEventListener("click", () => loadExample(EXAMPLE_PASS));
  $("btn-example-fail").addEventListener("click", () => loadExample(EXAMPLE_FAIL));
  $("btn-clear").addEventListener("click", clearAll);
  ["in-source", "in-sink", "in-required", "in-window"].forEach((id) =>
    $(id).addEventListener("input", markDirty)
  );

  renderAll();
})();
