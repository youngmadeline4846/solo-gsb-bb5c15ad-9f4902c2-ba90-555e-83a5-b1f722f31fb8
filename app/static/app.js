/* 样本交接链路审计 —— 前端逻辑（原生 JS，无框架） */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  const state = {
    samples: [],
    currentId: null,
    data: null, // { sample, events, audit, signoffs, handovers, location, location_moves }
    locations: [],       // 库位列表缓存（目标库位下拉与乐观锁版本来源）
    editingId: null,
    viewSignoffId: null, // 当前弹窗中查看的签署快照 id
    confirmPair: null,   // 当前确认弹窗对应的交接项 { fromId, toId }
    view: "sample",      // 主区域当前视图：sample（样本详情）| queue（异常处置队列）| locations（库位管理）
    queue: {
      filters: { status: "open", code: "", custodian: "", time_from: "", time_to: "", claim: "" },
      data: null,        // 最近一次队列响应
      dispose: null,     // 当前处置弹窗目标 { fp, version, action }
      claim: null,       // 当前认领弹窗目标 { fp, version, mode }
      countdownTimer: null, // 剩余时间倒计时 / 到期自动刷新
    },
  };

  // ---------- 工具 ----------

  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (isError ? " error" : "");
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (el.hidden = true), 2600);
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (resp.status === 204) return null;
    let payload = null;
    try {
      payload = await resp.json();
    } catch (_) {
      /* 非 JSON 响应 */
    }
    if (!resp.ok) {
      const err = new Error(extractError(payload) || `请求失败（${resp.status}）`);
      err.status = resp.status;
      throw err;
    }
    return payload;
  }

  function extractError(payload) {
    if (!payload) return "";
    if (typeof payload.detail === "string") return payload.detail;
    if (Array.isArray(payload.detail)) {
      return payload.detail
        .map((d) => {
          const field = d.loc && d.loc.length > 1 ? d.loc[d.loc.length - 1] : "";
          return field ? `【${field}】${d.msg}` : d.msg;
        })
        .join("；");
    }
    return payload.detail || "";
  }

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);

  // 存储格式 "YYYY-MM-DD HH:MM:SS" -> datetime-local 格式
  const toLocalInput = (s) => (s ? s.replace(" ", "T").slice(0, 19) : "");

  // 当前本地时间 -> datetime-local 格式（签署时间默认值）
  const nowLocalInput = () => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  };

  const STATUS_LABEL = { pass: "审计通过", warning: "通过 · 有告警", fail: "审计未通过", empty: "暂无数据" };
  const FINDING_LABEL = {
    gap: "断档", overlap: "重叠", reversed_interval: "起止倒置",
    out_of_order: "逆序录入", bad_time: "时间错误",
  };
  const SIGNOFF_STATUS_LABEL = { active: "已签署", revoked: "已撤销" };
  const HANDOVER_STATUS_LABEL = {
    confirmed: "已确认", pending: "待确认", time_mismatch: "时间不一致",
  };
  const HANDOVER_REASON_LABEL = {
    event_edited: "关联事件被编辑，绑定字段已变化",
    event_deleted: "关联事件已被删除",
    adjacency_changed: "两条记录不再相邻",
  };
  const FINDING_STATUS_LABEL = { open: "未处理", resolved: "已处理", eliminated: "已消除" };
  const CLAIM_STATUS_LABEL = { unclaimed: "未认领", claimed: "认领中", expired: "已过期" };
  const CLAIM_EVENT_LABEL = {
    state: "状态迁移", claim: "限时认领", release: "主动释放",
    takeover: "过期接管", lease_void: "租约失效",
  };

  // ---------- 样本列表 ----------

  async function loadSamples(selectId = null) {
    state.samples = await api("/api/samples");
    const ul = $("#sample-list");
    if (!state.samples.length) {
      ul.innerHTML = `<li class="muted-cell" style="cursor:default">暂无样本，点击右上角新建</li>`;
    } else {
      ul.innerHTML = state.samples
        .map(
          (s) => `
        <li data-id="${s.id}" class="${s.id === state.currentId ? "active" : ""}">
          <div class="s-code">${esc(s.code)}</div>
          <div class="s-name">${esc(s.name)}</div>
          <span class="s-count">${s.event_count} 条交接</span>
        </li>`
        )
        .join("");
    }
    if (selectId) selectSample(selectId);
    else if (state.currentId && !state.samples.some((s) => s.id === state.currentId)) {
      state.currentId = null;
      showEmpty();
    }
    loadQueueBadge(); // 未处理数角标随样本 / 链路变化刷新
  }

  // 主区域视图切换：样本详情 <-> 异常处置队列 / 库位管理（跨样本）
  function setView(view) {
    state.view = view;
    $("#btn-queue").classList.toggle("active", view === "queue");
    $("#btn-locations").classList.toggle("active", view === "locations");
    $("#queue-view").hidden = view !== "queue";
    $("#locations-view").hidden = view !== "locations";
    if (view !== "queue") {
      // 离开队列视图时停止租约倒计时
      clearInterval(state.queue.countdownTimer);
      state.queue.countdownTimer = null;
    }
    if (view === "queue" || view === "locations") {
      $("#detail").hidden = true;
      $("#empty-state").hidden = true;
      document
        .querySelectorAll("#sample-list li")
        .forEach((li) => li.classList.remove("active"));
    }
  }

  $("#sample-list").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-id]");
    if (li) selectSample(Number(li.dataset.id));
  });

  function showEmpty() {
    setView("sample");
    $("#detail").hidden = true;
    $("#empty-state").hidden = false;
  }

  async function selectSample(id) {
    try {
      state.currentId = id;
      const [detail, locations] = await Promise.all([
        api(`/api/samples/${id}`),
        api("/api/locations"),
      ]);
      state.data = detail;
      state.locations = locations;
      state.editingId = null;
      linState.rows = [];
      $("#aliquot-form").reset();
      setView("sample");
      renderAll();
      $("#empty-state").hidden = true;
      $("#detail").hidden = false;
      document.querySelectorAll("#sample-list li").forEach((li) =>
        li.classList.toggle("active", Number(li.dataset.id) === id)
      );
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ---------- 渲染 ----------

  function renderAll() {
    renderSample();
    renderLineage();
    renderLocationCard();
    renderAudit();
    renderHandovers();
    renderTimeline();
    renderEvents();
    renderSignoffs();
    renderSeal();
    resetForm();
  }

  function renderSample() {
    const { sample } = state.data;
    $("#d-code").textContent = sample.code;
    $("#d-name").textContent = sample.name;
    $("#d-desc").textContent = sample.description || "（无描述）";
  }

  function renderAudit() {
    const { audit } = state.data;
    const badge = $("#audit-badge");
    badge.textContent = STATUS_LABEL[audit.status] || audit.status;
    badge.className = `badge badge-${audit.status}`;
    $("#audit-conclusion").textContent = audit.conclusion;

    const s = audit.summary;
    const stat = (num, lbl, cls = "") =>
      `<div class="stat ${cls}"><div class="num">${num}</div><div class="lbl">${lbl}</div></div>`;
    $("#audit-stats").innerHTML = [
      stat(s.total, "交接记录"),
      stat(s.gaps, "断档", s.gaps ? "bad" : ""),
      stat(s.overlaps, "重叠", s.overlaps ? "bad" : ""),
      stat(s.reversed, "起止倒置", s.reversed ? "bad" : ""),
      stat(s.out_of_order, "逆序录入", s.out_of_order ? "warn" : ""),
      stat(s.open_holds, "未归还", s.open_holds ? "warn" : ""),
    ].join("");

    const fl = $("#findings");
    if (!audit.findings.length) {
      fl.innerHTML = `<li class="muted-item">✓ 未发现断档、重叠或逆序问题。</li>`;
    } else {
      fl.innerHTML = audit.findings
        .map(
          (f, i) => `
        <li class="finding-${f.code}" data-ids="${(f.event_ids || []).join(",")}">
          <span class="tag">${FINDING_LABEL[f.code] || f.code}</span>
          <span>${esc(f.message)}</span>
        </li>`
        )
        .join("");
    }
  }

  function renderTimeline() {
    const tl = state.data.audit.timeline;
    const wrap = $("#timeline");
    if (!tl.bars.length) {
      wrap.innerHTML = `<div class="tl-empty">暂无交接事件，登记后将在此显示时间线。</div>`;
      return;
    }

    // 泳道
    const lanes = [];
    for (let i = 0; i < tl.lanes; i++) {
      lanes.push(`<div class="tl-lane"></div>`);
    }

    // 冲突区域贯穿所有泳道
    const zones = tl.zones
      .map(
        (z) => `
      <div class="tl-zone ${z.kind}" style="left:${z.left}%;width:${z.width}%"
           title="${z.kind === "gap" ? "保管断档" : "交接重叠"}：${esc(z.from)} ~ ${esc(z.to)}">
        ${z.kind === "gap" ? "断档" : "重叠"}
      </div>`
      )
      .join("");

    // 事件条
    const bars = tl.bars
      .map((b) => {
        const cls = ["tl-bar"];
        if (b.conflict) cls.push("conflict");
        if (b.open) cls.push("open");
        const tip = `${b.custodian}\n${b.start_label} ~ ${b.end_label}${
          b.conflict ? "\n⚠ 该事件涉及审计冲突" : ""
        }`;
        return `
        <div class="${cls.join(" ")}" data-eid="${b.id}"
             style="left:${b.left}%;width:${b.width}%;top:${b.lane * 42 + 6}px"
             title="${esc(tip)}">
          <span class="bar-id">#${b.id}</span>${esc(b.custodian)}${b.open ? " ◷" : ""}
        </div>`;
      })
      .join("");

    const ticks = tl.ticks
      .map((t) => `<span class="tl-tick" style="left:${t.left}%">${esc(t.label)}</span>`)
      .join("");

    wrap.innerHTML = `
      <div class="tl-scroll">
        <div class="tl-lanes" style="height:${tl.lanes * 42}px">
          <div class="tl-zones">${zones}</div>
          ${bars}
          ${lanes.join("")}
        </div>
        <div class="tl-axis">${ticks}</div>
        <div class="tl-range">时间范围：${esc(tl.start)} ~ ${esc(tl.end)}（仍在保管的事件以斜纹延伸显示）</div>
      </div>`;
  }

  function attachmentsForEvent(eventId) {
    return (state.data.attachments || []).filter((a) => a.event_id === eventId);
  }

  function renderEvents() {
    const { audit } = state.data;
    const tbody = $("#event-tbody");
    if (!audit.events.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted-cell" style="text-align:center;padding:22px">暂无交接事件</td></tr>`;
      return;
    }
    tbody.innerHTML = audit.events
      .map((e, idx) => {
        const end = e.open
          ? `<span class="open-tag">仍在保管</span><div class="muted-cell">自 ${esc(e.start_time)} 起</div>`
          : esc(e.end_time);
        const atts = attachmentsForEvent(e.id);
        const corruptCount = atts.filter((a) => a.status === "corrupt").length;
        const attBtn = atts.length
          ? `📎 附件 <span class="att-count${corruptCount ? " att-corrupt" : ""}">${atts.length}${
              corruptCount ? ` · ${corruptCount} 损坏` : ""
            }</span>`
          : "📎 附件";
        return `
        <tr class="${e.has_conflict ? "row-conflict" : ""}" data-eid="${e.id}">
          <td class="col-ord">${idx + 1}</td>
          <td class="col-seq">${e.seq}</td>
          <td>${e.has_conflict ? '<span class="conflict-flag">⚠</span>' : ""}${esc(e.custodian)}</td>
          <td>${esc(e.start_time)}</td>
          <td>${end}</td>
          <td>${esc(e.duration)}</td>
          <td>${esc(e.note || "—")}</td>
          <td style="white-space:nowrap">
            <button class="link-btn btn-edit" data-act="edit" data-id="${e.id}">编辑</button>
            <button class="link-btn btn-del" data-act="delete" data-id="${e.id}">删除</button>
            <button class="link-btn btn-edit" data-act="attachments" data-id="${e.id}">${attBtn}</button>
          </td>
        </tr>`;
      })
      .join("");
  }

  // 点击审计发现 -> 高亮相关事件条与表格行
  $("#findings").addEventListener("click", (e) => {
    const li = e.target.closest("li[data-ids]");
    if (!li) return;
    const ids = li.dataset.ids.split(",").filter(Boolean).map(Number);
    ids.forEach((id) => {
      document.querySelectorAll(`[data-eid="${id}"]`).forEach((el) => {
        el.style.outline = "2px solid #f59e0b";
        setTimeout(() => (el.style.outline = ""), 1800);
      });
    });
  });

  // ---------- 双向交接确认 ----------

  function renderHandovers() {
    const hv = state.data.handovers || { items: [], history: [], summary: {} };
    const s = hv.summary || {};
    $("#handover-stats").innerHTML = [
      statChip(s.total ?? 0, "交接项", ""),
      statChip(s.confirmed ?? 0, "已确认", "ok"),
      statChip(s.pending ?? 0, "待确认", "muted"),
      statChip(s.time_mismatch ?? 0, "时间不一致", "bad"),
      statChip(s.invalidated ?? 0, "失效历史", "warn"),
    ].join("");

    const tbody = $("#handover-tbody");
    if (!hv.items || !hv.items.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="muted-cell" style="text-align:center;padding:22px">至少需要两条按时间相邻的交接事件才会生成交接项</td></tr>`;
    } else {
      tbody.innerHTML = hv.items.map((item) => renderHandoverRow(item)).join("");
    }

    const historyWrap = $("#handover-history-wrap");
    if (hv.history && hv.history.length) {
      historyWrap.hidden = false;
      $("#handover-history-tbody").innerHTML = hv.history
        .map(
          (h) => `
        <tr class="row-invalid">
          <td>${esc(h.from_custodian || "（事件已删除）")}<div class="muted-cell">${esc(h.from_end_time || "—")}</div></td>
          <td>${esc(h.to_custodian || "（事件已删除）")}<div class="muted-cell">${esc(h.to_start_time || "—")}</div></td>
          <td>${h.from_name ? esc(h.from_name) + `<div class="muted-cell">${esc(h.from_confirmed_at)}</div>` : '<span class="muted-cell">—</span>'}</td>
          <td>${h.to_name ? esc(h.to_name) + `<div class="muted-cell">${esc(h.to_confirmed_at)}</div>` : '<span class="muted-cell">—</span>'}</td>
          <td>${esc(HANDOVER_REASON_LABEL[h.reason] || h.reason_text || h.reason)}</td>
          <td class="muted-cell">${esc(h.invalidated_at)}</td>
          <td><button class="link-btn btn-edit" data-ho-detail="${h.id}">查看记录</button></td>
        </tr>`
        )
        .join("");
    } else {
      historyWrap.hidden = true;
    }
  }

  function statChip(num, label, cls) {
    return `<span class="ho-stat ${cls}"><b>${num}</b> ${label}</span>`;
  }

  function renderHandoverRow(item) {
    const a = item.from_event;
    const b = item.to_event;
    const conf = item.confirmation;
    const badge =
      item.status === "confirmed"
        ? `<span class="badge badge-pass">已确认</span>`
        : item.status === "time_mismatch"
        ? `<span class="badge badge-fail">时间不一致</span>`
        : `<span class="badge badge-empty">待确认</span>`;

    const link = item.time_consistent
      ? `<span class="ho-link ok">结束 = 开始</span>`
      : `<span class="ho-link bad">结束 ≠ 开始</span>`;
    const at = a.end_time
      ? `${esc(a.end_time)}`
      : `<span class="open-tag">交出方仍在保管（无结束时间）</span>`;

    const sideCell = (name, time, role) =>
      name
        ? `<span class="ho-done">${esc(name)}</span><div class="muted-cell">${role} · ${esc(time)}</div>`
        : `<span class="muted-cell">${role}待确认</span>`;
    const fromSide = sideCell(conf?.from_name, conf?.from_confirmed_at, "交出");
    const toSide = sideCell(conf?.to_name, conf?.to_confirmed_at, "接收");

    const invalidHint = item.status === "time_mismatch"
      ? `<div class="muted-cell ho-hint">两侧已确认，但交出结束时间（${esc(a.end_time || "—")}）与接收开始时间（${esc(b.start_time)}）不一致，请核对后重新确认</div>`
      : item.status === "pending"
      ? `<div class="muted-cell ho-hint">${link}${conf ? "，尚有一侧未确认" : "，两侧均未确认"}</div>`
      : `<div class="muted-cell ho-hint">绑定摘要 <code class="digest">${esc(conf?.digest || "")}</code></div>`;

    return `
    <tr class="ho-row ho-${item.status}">
      <td class="col-ord">${item.index}</td>
      <td>${esc(a.custodian)}<div class="muted-cell">至 ${at}</div>${fromSide}</td>
      <td>${esc(b.custodian)}<div class="muted-cell">始于 ${esc(b.start_time)}</div>${toSide}</td>
      <td>${at}</td>
      <td>${badge}${invalidHint}</td>
      <td>
        <button class="link-btn btn-edit" data-ho-from="${a.id}" data-ho-to="${b.id}">
          ${conf ? "补充 / 重新确认" : "填写确认"}
        </button>
        ${conf ? `<button class="link-btn btn-edit" data-ho-detail="${conf.id}">查看记录</button>` : ""}
      </td>
    </tr>`;
  }

  // 打开确认弹窗
  $("#handover-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-ho-from]");
    if (!btn) return;
    const fromId = Number(btn.dataset.hoFrom);
    const toId = Number(btn.dataset.hoTo);
    openHandoverModal(fromId, toId);
  });

  function openHandoverModal(fromId, toId) {
    const hv = state.data.handovers;
    const item = hv.items.find(
      (i) => i.from_event.id === fromId && i.to_event.id === toId
    );
    if (!item) return;
    state.confirmPair = { fromId, toId };
    const a = item.from_event;
    const b = item.to_event;
    $("#handover-modal-body").innerHTML = `
      <div class="ho-modal-pair ${item.time_consistent ? "ok" : "bad"}">
        <div><span class="meta-lbl">交出方</span>${esc(a.custodian)}<div class="muted-cell">结束：${esc(a.end_time || "仍在保管")}</div></div>
        <div class="ho-arrow">→</div>
        <div><span class="meta-lbl">接收方</span>${esc(b.custodian)}<div class="muted-cell">开始：${esc(b.start_time)}</div></div>
      </div>
      <p class="muted-cell ho-modal-hint">
        ${item.time_consistent ? "交接时点一致。" : "交出结束时间与接收开始时间不一致，两侧确认后将标记为「时间不一致」。"}
        确认会绑定两条事件当前的经手人、起止时间与备注摘要；任一事件后续被编辑、删除或不再相邻时，原确认保留为失效记录并需重新确认。
      </p>`;
    $("#handover-form").reset();
    $("#ho-at").value = nowLocalInput();
    $("#handover-modal").hidden = false;
    $("#ho-name").focus();
  }

  const handoverModal = $("#handover-modal");
  $("#btn-close-handover").addEventListener("click", () => (handoverModal.hidden = true));
  handoverModal.addEventListener("click", (e) => {
    if (e.target === handoverModal) handoverModal.hidden = true;
  });

  $("#handover-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.confirmPair) return;
    const side = document.querySelector('input[name="ho-side"]:checked').value;
    const payload = {
      side,
      confirmer_name: $("#ho-name").value.trim(),
      confirmed_at: $("#ho-at").value,
    };
    if (!payload.confirmer_name || !payload.confirmed_at) {
      toast("请填写确认人姓名与确认时间", true);
      return;
    }
    try {
      const { fromId, toId } = state.confirmPair;
      const result = await api(
        `/api/samples/${state.currentId}/handovers/confirm?from_event_id=${fromId}&to_event_id=${toId}`,
        { method: "POST", body: JSON.stringify(payload) }
      );
      state.data.handovers = result.view;
      renderHandovers();
      const item = result.item;
      const otherDone =
        item.status === "confirmed" || item.status === "time_mismatch";
      toast(
        otherDone
          ? item.status === "confirmed"
            ? "两侧均已确认，交接项已确认 ✓"
            : "两侧均已确认，但交接时间不一致"
          : "确认已提交，等待另一侧确认"
      );
      if (otherDone) handoverModal.hidden = true;
    } catch (err) {
      toast(err.message, true);
    }
  });

  // 交接确认记录详情（含失效历史）
  async function openHandoverDetail(id) {
    try {
      const d = await api(`/api/handovers/${id}`);
      const snapA = d.from_event_snapshot || {};
      const snapB = d.to_event_snapshot || {};
      const badge =
        d.status === "active"
          ? `<span class="badge badge-pass">有效</span>`
          : `<span class="badge badge-fail">已失效</span>`;
      const invalid =
        d.status === "invalidated"
          ? `<div class="revoked-info">⚠ 该确认已于 <b>${esc(d.invalidated_at || "")}</b> 失效：
             ${esc(d.invalidated_reason_text || "")}。原记录保留备查，不再计入有效状态，需要两侧重新确认。</div>`
          : "";
      const sideRow = (title, name, at) => `
        <tr><th>${title}</th>
            <td>${name ? esc(name) : '<span class="muted-cell">待确认</span>'}</td>
            <td class="muted-cell">${at ? esc(at) : "—"}</td></tr>`;
      $("#handover-detail-body").innerHTML = `
        <div class="signoff-meta">
          <div><span class="meta-lbl">状态</span>${badge}</div>
          <div><span class="meta-lbl">绑定摘要</span><code class="digest">${esc(d.digest.slice(0, 12))}</code></div>
          <div><span class="meta-lbl">创建时间</span>${esc(d.created_at)}</div>
          <div><span class="meta-lbl">最近更新</span>${esc(d.updated_at)}</div>
        </div>
        <div class="signoff-section">
          <h4>双方确认</h4>
          <div class="table-wrap">
            <table class="event-table">
              <thead><tr><th>角色</th><th>确认人</th><th>确认时间</th></tr></thead>
              <tbody>
                ${sideRow("交出方（事件 #" + d.from_event_id + "）", d.from_name, d.from_confirmed_at)}
                ${sideRow("接收方（事件 #" + d.to_event_id + "）", d.to_name, d.to_confirmed_at)}
              </tbody>
            </table>
          </div>
        </div>
        <div class="signoff-section">
          <h4>确认时绑定的事件关键字段摘要</h4>
          <div class="table-wrap">
            <table class="event-table">
              <thead><tr><th>角色</th><th>经手人</th><th>起始时间</th><th>结束时间</th><th>备注</th></tr></thead>
              <tbody>
                <tr><td>交出方</td><td>${esc(snapA.custodian)}</td><td>${esc(snapA.start_time)}</td>
                    <td>${esc(snapA.end_time || "仍在保管")}</td><td>${esc(snapA.note || "—")}</td></tr>
                <tr><td>接收方</td><td>${esc(snapB.custodian)}</td><td>${esc(snapB.start_time)}</td>
                    <td>${esc(snapB.end_time || "仍在保管")}</td><td>${esc(snapB.note || "—")}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
        ${invalid}`;
      $("#handover-detail-modal").hidden = false;
    } catch (err) {
      toast(err.message, true);
    }
  }

  $("#handover-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-ho-detail]");
    if (btn) openHandoverDetail(Number(btn.dataset.hoDetail));
  });
  $("#handover-history-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-ho-detail]");
    if (btn) openHandoverDetail(Number(btn.dataset.hoDetail));
  });
  const handoverDetailModal = $("#handover-detail-modal");
  $("#btn-close-handover-detail").addEventListener("click", () => (handoverDetailModal.hidden = true));
  handoverDetailModal.addEventListener("click", (e) => {
    if (e.target === handoverDetailModal) handoverDetailModal.hidden = true;
  });

  // ---------- 异常处置队列（跨样本） ----------

  async function loadQueueBadge() {
    try {
      const s = await api("/api/findings/summary");
      updateQueueBadge(s);
    } catch (_) {
      /* 角标刷新失败不阻塞主流程 */
    }
  }

  function updateQueueBadge(summary) {
    const badge = $("#queue-badge");
    const n = (summary && summary.open) || 0;
    badge.textContent = n > 99 ? "99+" : String(n);
    badge.hidden = n === 0;
  }

  $("#btn-queue").addEventListener("click", () => {
    setView("queue");
    loadQueue();
  });

  async function loadQueue() {
    const f = state.queue.filters;
    const params = new URLSearchParams();
    params.set("status", f.status);
    if (f.code) params.set("code", f.code);
    if (f.custodian) params.set("custodian", f.custodian);
    if (f.time_from) params.set("time_from", f.time_from);
    if (f.time_to) params.set("time_to", f.time_to);
    if (f.claim) params.set("claim", f.claim);
    try {
      const data = await api(`/api/findings/queue?${params.toString()}`);
      state.queue.data = data;
      renderQueue();
      updateQueueBadge(data.summary);
    } catch (err) {
      toast(err.message, true);
    }
  }

  function fmtRemaining(sec) {
    if (sec == null) return "";
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    if (m <= 0) return `剩余 ${s} 秒`;
    return `剩余 ${m} 分 ${String(s).padStart(2, "0")} 秒`;
  }

  function renderQueue() {
    const data = state.queue.data;
    if (!data) return;
    const s = data.summary || {};
    $("#queue-stats").innerHTML = [
      statChip(s.open ?? 0, "未处理", s.open ? "bad" : ""),
      statChip(s.claimed ?? 0, "认领中", "warn"),
      statChip(s.resolved ?? 0, "已处理", "ok"),
      statChip(s.eliminated ?? 0, "已消除", "muted"),
      statChip(s.total ?? 0, "累计发现", ""),
    ].join("");
    $("#queue-synced").textContent = data.synced_at
      ? `已按当前链路重算 · ${data.synced_at}`
      : "";

    // 经手人候选（跨样本去重，由服务端返回）
    $("#q-custodian-list").innerHTML = (data.custodians || [])
      .map((c) => `<option value="${esc(c)}"></option>`)
      .join("");

    const tbody = $("#queue-tbody");
    if (!data.items.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="muted-cell" style="text-align:center;padding:22px">当前筛选条件下没有审计发现</td></tr>`;
    } else {
      tbody.innerHTML = data.items.map(renderQueueRow).join("");
    }
    scheduleLeaseCountdown(data);
  }

  function renderClaimCell(it) {
    if (it.status !== "open") {
      return `<span class="muted-cell">—</span>`;
    }
    if (it.claim_state === "claimed") {
      return `<span class="badge badge-warn claim-badge">认领中</span>
        <div class="muted-cell">${esc(it.claimed_by || "")}</div>
        <div class="claim-remain" data-expires="${esc(it.claim_expires_at || "")}">
          ${esc(fmtRemaining(it.claim_remaining_seconds))}
        </div>
        <div class="muted-cell">至 ${esc(it.claim_expires_at || "")}</div>`;
    }
    if (it.claim_state === "expired") {
      return `<span class="badge badge-fail claim-badge">已过期·可接管</span>
        <div class="muted-cell">原认领 ${esc(it.claimed_by || "")}</div>
        <div class="muted-cell">至 ${esc(it.claim_expires_at || "")}</div>`;
    }
    return `<span class="badge badge-empty claim-badge">未认领</span>`;
  }

  function renderQueueRow(it) {
    const statusBadge =
      it.status === "open"
        ? `<span class="badge badge-fail">未处理</span>`
        : it.status === "resolved"
        ? `<span class="badge badge-pass">已处理</span>`
        : `<span class="badge badge-empty">已消除</span>`;
    const timeRange = `${esc(it.time_from || "—")}<div class="muted-cell">至 ${esc(
      it.time_to || "开放（仍在保管）"
    )}</div>`;
    const disposeInfo =
      it.status === "eliminated"
        ? `<span class="muted-cell">链路变更后消除</span><div class="muted-cell">${esc(
            it.eliminated_at || ""
          )}</div>`
        : it.handler
        ? `${esc(it.handler)}${
            it.note ? `<div class="muted-cell">${esc(it.note)}</div>` : ""
          }<div class="muted-cell">${esc(it.resolved_at || it.updated_at || "")}</div>`
        : `<span class="muted-cell">—</span>`;
    const actions = [];
    if (it.status === "open") {
      if (it.claim_state === "claimed") {
        actions.push(
          `<button class="link-btn btn-edit" data-dispose="resolve" data-fp="${it.fingerprint}" data-ver="${it.version}" data-claim="${esc(it.claimed_by || "")}">处置</button>`,
          `<button class="link-btn btn-edit" data-release="${it.fingerprint}" data-ver="${it.version}">释放</button>`
        );
      } else if (it.claim_state === "expired") {
        actions.push(
          `<button class="link-btn btn-edit" data-claim="${it.fingerprint}" data-ver="${it.version}" data-mode="takeover" data-owner="${esc(it.claimed_by || "")}">接管</button>`
        );
      } else {
        actions.push(
          `<button class="link-btn btn-edit" data-claim="${it.fingerprint}" data-ver="${it.version}" data-mode="claim">认领</button>`
        );
      }
    } else if (it.status === "resolved") {
      actions.push(
        `<button class="link-btn btn-edit" data-dispose="reopen" data-fp="${it.fingerprint}" data-ver="${it.version}">重新打开</button>`
      );
    }
    actions.push(
      `<button class="link-btn btn-edit" data-history="${it.fingerprint}">迁移记录</button>`
    );
    const rowCls =
      it.status === "open" ? "row-conflict" : it.status === "eliminated" ? "row-invalid" : "";
    return `
      <tr class="${rowCls}">
        <td><div class="s-code">${esc(it.sample.code)}</div><div class="muted-cell">${esc(it.sample.name)}</div></td>
        <td><span class="finding-tag finding-tag-${esc(it.code)}">${esc(FINDING_LABEL[it.code] || it.code)}</span></td>
        <td>${esc(it.message)}<div class="muted-cell">指纹 <code class="digest">${esc(
          it.fingerprint.slice(0, 12)
        )}</code> · v${it.version}</div></td>
        <td>${it.custodians.map(esc).join("、") || "—"}</td>
        <td>${timeRange}</td>
        <td>${statusBadge}<div class="muted-cell">首次 ${esc(it.first_seen_at)}</div></td>
        <td>${renderClaimCell(it)}</td>
        <td>${disposeInfo}</td>
        <td style="white-space:nowrap">${actions.join("")}</td>
      </tr>`;
  }

  // 租约倒计时：每秒刷新剩余时间；最近一条未到期租约到期时自动重排队列
  function scheduleLeaseCountdown(data) {
    clearInterval(state.queue.countdownTimer);
    const items = (data && data.items) || [];
    let nextExpiry = Infinity;
    const now = Date.now();
    for (const it of items) {
      if (it.claim_state !== "claimed" || !it.claim_expires_at) continue;
      const t = new Date(it.claim_expires_at.replace(" ", "T")).getTime();
      if (Number.isFinite(t) && t > now && t < nextExpiry) nextExpiry = t;
    }
    state.queue.countdownTimer = setInterval(() => {
      const remainCells = document.querySelectorAll(".claim-remain[data-expires]");
      let nearest = Infinity;
      remainCells.forEach((el) => {
        const t = new Date(el.dataset.expires.replace(" ", "T")).getTime();
        const left = Math.max(0, Math.floor((t - Date.now()) / 1000));
        el.textContent = fmtRemaining(left);
        if (t > Date.now() && t < nearest) nearest = t;
      });
      // 页面上最近一条租约到期 -> 行操作按钮需切换（认领/接管），重新拉取队列
      if (!document.hidden && Number.isFinite(nearest) && nearest <= Date.now()) {
        loadQueue();
      }
    }, 1000);
  }

  // 筛选表单
  $("#queue-filter-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.queue.filters = {
      status: $("#q-status").value,
      code: $("#q-code").value,
      custodian: $("#q-custodian").value.trim(),
      time_from: $("#q-from").value,
      time_to: $("#q-to").value,
      claim: $("#q-claim").value,
    };
    loadQueue();
  });
  $("#btn-queue-reset").addEventListener("click", () => {
    $("#queue-filter-form").reset();
    $("#q-status").value = "open";
    $("#q-claim").value = "";
    state.queue.filters = { status: "open", code: "", custodian: "", time_from: "", time_to: "", claim: "" };
    loadQueue();
  });
  $("#btn-queue-refresh").addEventListener("click", loadQueue);

  // 队列行操作：认领 / 接管 / 释放 / 处置 / 重新打开 / 迁移记录
  $("#queue-tbody").addEventListener("click", (e) => {
    const claimBtn = e.target.closest("button[data-claim][data-mode]");
    if (claimBtn) {
      openClaimModal(
        claimBtn.dataset.claim,
        Number(claimBtn.dataset.ver),
        claimBtn.dataset.mode,
        claimBtn.dataset.owner || ""
      );
      return;
    }
    const releaseBtn = e.target.closest("button[data-release]");
    if (releaseBtn) {
      openClaimModal(
        releaseBtn.dataset.release,
        Number(releaseBtn.dataset.ver),
        "release",
        ""
      );
      return;
    }
    const disposeBtn = e.target.closest("button[data-dispose]");
    if (disposeBtn) {
      openDisposeModal(
        disposeBtn.dataset.fp,
        Number(disposeBtn.dataset.ver),
        disposeBtn.dataset.dispose,
        disposeBtn.dataset.claim || ""
      );
      return;
    }
    const histBtn = e.target.closest("button[data-history]");
    if (histBtn) openFindingHistory(histBtn.dataset.history);
  });

  function findingByFp(fp) {
    const items = (state.queue.data && state.queue.data.items) || [];
    return items.find((x) => x.fingerprint === fp);
  }

  // ---------- 限时认领 / 接管 / 释放弹窗 ----------

  function openClaimModal(fp, version, mode, owner) {
    const it = findingByFp(fp);
    if (!it) return;
    state.queue.claim = { fp, version, mode };
    const isRelease = mode === "release";
    const isTakeover = mode === "takeover";
    $("#claim-title").textContent =
      isRelease ? "主动释放认领" : isTakeover ? "接管过期认领" : "限时认领审计发现";
    $("#claim-body").innerHTML = `
      <div class="dispose-finding">
        <div><span class="meta-lbl">样本</span>${esc(it.sample.code)} · ${esc(it.sample.name)}</div>
        <div><span class="meta-lbl">类型</span>${esc(FINDING_LABEL[it.code] || it.code)}</div>
        <div><span class="meta-lbl">发现</span>${esc(it.message)}</div>
        <div><span class="meta-lbl">指纹版本</span><code class="digest">${esc(
          it.fingerprint.slice(0, 12)
        )}</code> · v${it.version}</div>
        ${
          it.claim_state === "claimed"
            ? `<div><span class="meta-lbl">当前认领</span>${esc(it.claimed_by)}（${esc(
                fmtRemaining(it.claim_remaining_seconds)
              )}，至 ${esc(it.claim_expires_at)}）</div>`
            : it.claim_state === "expired"
            ? `<div><span class="meta-lbl">原认领</span>${esc(
                it.claimed_by
              )}，租约已到期，可由他人接管（版本递增）</div>`
            : ""
        }
      </div>
      <p class="muted-cell ho-modal-hint">${
        isRelease
          ? "只有当前认领人可以主动释放；释放后其他人可立即认领。"
          : "认领与接管在服务端单事务内先重算发现、再同时校验指纹版本与租约状态；未到期被他人占用时返回 409。"
      }</p>`;
    $("#claim-handler-label").hidden = false;
    $("#cl-handler").value =
      isRelease && it.claimed_by ? it.claimed_by : "";
    $("#cl-handler").placeholder = isRelease
      ? "须为当前认领人"
      : isTakeover
      ? "接管人，如：审计员 李强"
      : "认领人，如：审计员 王敏";
    $("#claim-note-label").hidden = !isRelease;
    $("#cl-submit").textContent =
      isRelease ? "释放认领" : isTakeover ? "接管（续租 15 分钟）" : "认领（15 分钟）";
    $("#claim-form").reset();
    if (isRelease && it.claimed_by) $("#cl-handler").value = it.claimed_by;
    $("#claim-modal").hidden = false;
    $("#cl-handler").focus();
  }

  const claimModal = $("#claim-modal");
  $("#btn-close-claim").addEventListener("click", () => (claimModal.hidden = true));
  claimModal.addEventListener("click", (e) => {
    if (e.target === claimModal) claimModal.hidden = true;
  });

  $("#claim-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const c = state.queue.claim;
    if (!c) return;
    const handler = $("#cl-handler").value.trim();
    if (!handler) {
      toast("请填写认领人", true);
      return;
    }
    const payload = { expected_version: c.version, handler };
    const path =
      c.mode === "release"
        ? `/api/findings/${c.fp}/release`
        : `/api/findings/${c.fp}/claim`;
    if (c.mode === "release") payload.note = $("#cl-note").value.trim();
    try {
      await api(path, { method: "POST", body: JSON.stringify(payload) });
      claimModal.hidden = true;
      state.queue.claim = null;
      toast(
        c.mode === "release"
          ? "已释放认领"
          : c.mode === "takeover"
          ? "已接管，新租约 15 分钟"
          : "认领成功，租约 15 分钟"
      );
      await loadQueue();
    } catch (err) {
      if (err.status === 409 || err.status === 404) {
        claimModal.hidden = true;
        state.queue.claim = null;
        await loadQueue();
      }
      toast(err.message, true);
    }
  });

  function openDisposeModal(fp, version, action, currentClaim) {
    const items = (state.queue.data && state.queue.data.items) || [];
    const it = items.find((x) => x.fingerprint === fp);
    if (!it) return;
    state.queue.dispose = { fp, version, action };
    $("#dispose-title").textContent =
      action === "resolve" ? "处置审计发现（标记已处理）" : "重新打开审计发现";
    $("#dispose-body").innerHTML = `
      <div class="dispose-finding">
        <div><span class="meta-lbl">样本</span>${esc(it.sample.code)} · ${esc(it.sample.name)}</div>
        <div><span class="meta-lbl">类型</span>${esc(FINDING_LABEL[it.code] || it.code)}</div>
        <div><span class="meta-lbl">发现</span>${esc(it.message)}</div>
        <div><span class="meta-lbl">指纹版本</span><code class="digest">${esc(
          it.fingerprint.slice(0, 12)
        )}</code> · v${it.version}</div>
        ${
          action === "resolve" && it.claim_state === "claimed"
            ? `<div><span class="meta-lbl">当前认领</span>${esc(
                it.claimed_by
              )}（${esc(fmtRemaining(it.claim_remaining_seconds))}），仅当前认领人可标记已处理</div>`
            : ""
        }
      </div>
      <p class="muted-cell ho-modal-hint">提交时服务端将在单一事务内先重算发现，再同时校验指纹版本与认领租约；若期间链路、状态或认领已变化，将返回冲突并自动刷新队列。</p>`;
    $("#dp-submit").textContent = action === "resolve" ? "标记已处理" : "重新打开";
    $("#dispose-form").reset();
    if (action === "resolve" && currentClaim) $("#dp-handler").value = currentClaim;
    $("#dispose-modal").hidden = false;
    $("#dp-handler").focus();
  }

  const disposeModal = $("#dispose-modal");
  $("#btn-close-dispose").addEventListener("click", () => (disposeModal.hidden = true));
  disposeModal.addEventListener("click", (e) => {
    if (e.target === disposeModal) disposeModal.hidden = true;
  });

  $("#dispose-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const d = state.queue.dispose;
    if (!d) return;
    const payload = {
      action: d.action,
      expected_version: d.version,
      handler: $("#dp-handler").value.trim(),
      note: $("#dp-note").value.trim(),
    };
    if (!payload.handler) {
      toast("请填写处置人", true);
      return;
    }
    try {
      await api(`/api/findings/${d.fp}/dispose`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      disposeModal.hidden = true;
      state.queue.dispose = null;
      toast(d.action === "resolve" ? "已标记为已处理" : "已重新打开，恢复为未处理");
      await loadQueue();
    } catch (err) {
      // 指纹版本过期 / 状态冲突：提示并刷新队列（过期请求返回冲突并刷新）
      if (err.status === 409 || err.status === 404) {
        disposeModal.hidden = true;
        state.queue.dispose = null;
        await loadQueue();
        toast(err.message, true);
      } else {
        toast(err.message, true);
      }
    }
  });

  // 状态迁移记录
  async function openFindingHistory(fp) {
    try {
      const d = await api(`/api/findings/${fp}`);
      const rows = (d.transitions || [])
        .map((t) => {
          let move;
          if (t.event && t.event !== "state") {
            // 认领 / 释放 / 过期接管 / 租约失效：处置状态本身可能不变，
            // 以事件标签为主、状态迁移为副
            const evLabel = CLAIM_EVENT_LABEL[t.event] || t.event_text || t.event;
            move = `${esc(evLabel)}<div class="muted-cell">${
              t.from_status
                ? esc(FINDING_STATUS_LABEL[t.from_status] || t.from_status) +
                  " → " + esc(FINDING_STATUS_LABEL[t.to_status] || t.to_status)
                : "—"
            }</div>`;
          } else if (t.from_status) {
            move = `${FINDING_STATUS_LABEL[t.from_status] || t.from_status} → ${
              FINDING_STATUS_LABEL[t.to_status] || t.to_status
            }`;
          } else {
            move = `首次出现（${FINDING_STATUS_LABEL[t.to_status] || t.to_status}）`;
          }
          return `<tr>
            <td>${esc(t.created_at)}</td>
            <td>${move}</td>
            <td>${t.handler ? esc(t.handler) : '<span class="muted-cell">系统</span>'}</td>
            <td>${esc(t.note || "—")}</td>
          </tr>`;
        })
        .join("");
      const leaseInfo =
        d.status === "open"
          ? d.claim_state === "claimed"
            ? `<div><span class="meta-lbl">限时认领</span>${esc(d.claimed_by)} · ${esc(
                fmtRemaining(d.claim_remaining_seconds)
              )}（至 ${esc(d.claim_expires_at)}）</div>`
            : d.claim_state === "expired"
            ? `<div><span class="meta-lbl">限时认领</span>已过期，原认领 ${esc(
                d.claimed_by
              )}，可接管</div>`
            : `<div><span class="meta-lbl">限时认领</span>未认领（可认领 15 分钟）</div>`
          : "";
      $("#finding-history-body").innerHTML = `
        <div class="signoff-meta">
          <div><span class="meta-lbl">样本</span>${esc(d.sample.code)} · ${esc(d.sample.name)}</div>
          <div><span class="meta-lbl">异常类型</span>${esc(FINDING_LABEL[d.code] || d.code)}</div>
          <div><span class="meta-lbl">当前状态</span>${esc(
            FINDING_STATUS_LABEL[d.status] || d.status
          )}（v${d.version}）</div>
          ${leaseInfo}
          <div><span class="meta-lbl">稳定指纹</span><code class="digest">${esc(d.fingerprint)}</code></div>
          <div><span class="meta-lbl">首次出现</span>${esc(d.first_seen_at)}</div>
          <div><span class="meta-lbl">最近出现</span>${esc(d.last_seen_at)}</div>
        </div>
        <p class="conclusion">${esc(d.message)}</p>
        <div class="table-wrap">
          <table class="event-table">
            <thead><tr><th>时间</th><th>迁移事件</th><th>操作人</th><th>备注</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
      $("#finding-history-modal").hidden = false;
    } catch (err) {
      toast(err.message, true);
    }
  }

  const findingHistoryModal = $("#finding-history-modal");
  $("#btn-close-finding-history").addEventListener(
    "click",
    () => (findingHistoryModal.hidden = true)
  );
  findingHistoryModal.addEventListener("click", (e) => {
    if (e.target === findingHistoryModal) findingHistoryModal.hidden = true;
  });

  // ---------- 证据封存包 ----------

  const SEAL_ISSUE_TYPE_LABEL = {
    manifest: "清单异常", structure: "结构异常", path_traversal: "路径穿越",
    limit: "超出限制", missing: "缺失", extra: "多余",
    size_mismatch: "大小不一致", digest_mismatch: "摘要异常",
  };

  function renderSeal() {
    const atts = state.data.attachments || [];
    const corrupt = atts.filter((a) => a.status === "corrupt").length;
    $("#seal-export-hint").textContent = atts.length
      ? `当前样本共 ${atts.length} 个附件记录` +
        (corrupt ? `，其中 ${corrupt} 个损坏（导出时排除并登记进清单）` : "，均可封存")
      : "当前样本暂无附件，封存包将只包含清单与审计报告";
    $("#seal-export-error").hidden = true;
    $("#seal-export-ok").hidden = true;
  }

  $("#btn-seal-export").addEventListener("click", async () => {
    const errEl = $("#seal-export-error");
    const okEl = $("#seal-export-ok");
    errEl.hidden = true;
    okEl.hidden = true;
    const corrupt = (state.data.attachments || []).filter((a) => a.status === "corrupt").length;
    if (
      corrupt &&
      !confirm(
        `当前样本有 ${corrupt} 个附件已损坏，不会包含在封存包内（会在清单中登记为已排除）。仍要导出吗？`
      )
    )
      return;
    const btn = $("#btn-seal-export");
    btn.disabled = true;
    try {
      const resp = await fetch(`/api/samples/${state.currentId}/seal-package`);
      if (!resp.ok) {
        const payload = await resp.json().catch(() => null);
        throw new Error(extractError(payload) || `导出失败（${resp.status}）`);
      }
      const blob = await resp.blob();
      // 从 Content-Disposition 取服务端生成的文件名
      const cd = resp.headers.get("Content-Disposition") || "";
      const m = cd.match(/filename\*=UTF-8''([^;]+)/i);
      const fname = m ? decodeURIComponent(m[1]) : "seal_package.zip";
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      const n = resp.headers.get("X-Seal-Attachments") || "0";
      const x = Number(resp.headers.get("X-Seal-Excluded") || "0");
      okEl.textContent =
        `✓ 封存包已导出：${fname}（含 ${n} 个未损坏附件` +
        (x ? `，另排除 ${x} 个损坏附件并已登记进清单` : "") +
        "）。可用下方「校验封存包」复核。";
      okEl.hidden = false;
      toast("封存包已导出");
    } catch (err) {
      errEl.textContent = `✗ 导出失败：${err.message}`;
      errEl.hidden = false;
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-seal-verify").addEventListener("click", async () => {
    const fileInput = $("#seal-verify-file");
    const file = fileInput.files && fileInput.files[0];
    if (!file) {
      toast("请先选择要校验的封存包 ZIP 文件", true);
      return;
    }
    const box = $("#seal-verify-result");
    const btn = $("#btn-seal-verify");
    btn.disabled = true;
    try {
      const form = new FormData();
      form.append("file", file);
      const resp = await fetch("/api/seal-packages/verify", { method: "POST", body: form });
      const payload = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(extractError(payload) || `校验失败（${resp.status}）`);
      renderSealVerify(payload);
    } catch (err) {
      box.hidden = false;
      box.className = "seal-verify-result seal-bad";
      box.innerHTML = `<b>校验未完成：</b>${esc(err.message)}`;
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  function renderSealVerify(r) {
    const box = $("#seal-verify-result");
    box.hidden = false;
    box.className = "seal-verify-result " + (r.valid ? "seal-ok" : "seal-bad");
    const pkg = r.package || {};
    const counts = r.counts || {};
    const issues = r.issues || [];
    const issueItems = issues
      .map(
        (it) => `
      <li>
        <span class="seal-issue-type">${esc(SEAL_ISSUE_TYPE_LABEL[it.type] || it.type)}</span>
        <span class="seal-issue-msg">${esc(it.message)}</span>
        ${it.path ? `<code class="digest">${esc(it.path)}</code>` : ""}
      </li>`
      )
      .join("");
    box.innerHTML = `
      <div class="seal-verify-head">
        <span class="badge ${r.valid ? "badge-pass" : "badge-fail"}">${r.valid ? "校验通过" : "校验未通过"}</span>
        <span class="seal-summary">${esc(r.summary || "")}</span>
      </div>
      ${
        pkg.sample_code
          ? `<div class="muted-cell">包内样本：${esc(pkg.sample_code)}${
              pkg.sample_name ? " · " + esc(pkg.sample_name) : ""
            }　生成时间：${esc(pkg.generated_at || "—")}</div>`
          : ""
      }
      <div class="seal-counts">
        ${statChip(counts.declared_files ?? 0, "清单声明文件", "")}
        ${statChip(counts.checked_files ?? 0, "已核对", r.valid ? "ok" : "")}
        ${statChip(issues.length, "差异", issues.length ? "bad" : "muted")}
      </div>
      ${issueItems ? `<ul class="seal-issues">${issueItems}</ul>` : ""}`;
  }

  // ---------- 事件增 / 改 / 删 ----------

  function resetForm() {
    $("#event-form").reset();
    $("#f-event-id").value = "";
    state.editingId = null;
    $("#btn-submit-event").textContent = "登记交接事件";
    $("#btn-cancel-edit").hidden = true;
  }

  $("#btn-cancel-edit").addEventListener("click", resetForm);

  $("#event-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      custodian: $("#f-custodian").value.trim(),
      start_time: $("#f-start").value,
      end_time: $("#f-end").value || null,
      note: $("#f-note").value.trim(),
    };
    if (!payload.custodian || !payload.start_time) {
      toast("请填写经手人与起始时间", true);
      return;
    }
    try {
      if (state.editingId) {
        await api(`/api/events/${state.editingId}`, { method: "PUT", body: JSON.stringify(payload) });
        toast("交接事件已更新");
      } else {
        await api(`/api/samples/${state.currentId}/events`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        toast("交接事件已登记");
      }
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("#event-tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);

    if (btn.dataset.act === "edit") {
      const ev = state.data.events.find((x) => x.id === id);
      if (!ev) return;
      state.editingId = id;
      $("#f-event-id").value = id;
      $("#f-custodian").value = ev.custodian;
      $("#f-start").value = toLocalInput(ev.start_time);
      $("#f-end").value = toLocalInput(ev.end_time);
      $("#f-note").value = ev.note || "";
      $("#btn-submit-event").textContent = "保存修改";
      $("#btn-cancel-edit").hidden = false;
      $("#f-custodian").scrollIntoView({ behavior: "smooth", block: "center" });
      $("#f-custodian").focus();
      return;
    }

    if (btn.dataset.act === "delete") {
      if (attachedCount(id) > 0) {
        toast("该事件存在证据附件，请先在「📎 附件」中处理（删除）附件后再删除事件", true);
        return;
      }
      if (!confirm("确定删除这条交接事件？删除后将重新审计链路。")) return;
      try {
        await api(`/api/events/${id}`, { method: "DELETE" });
        toast("事件已删除");
        await refresh();
      } catch (err) {
        toast(err.message, true);
      }
    }

    if (btn.dataset.act === "attachments") {
      openAttachmentModal(id);
    }
  });

  function attachedCount(eventId) {
    return attachmentsForEvent(eventId).length;
  }

  async function refresh() {
    await loadSamples(state.currentId); // 事件数更新
    if (state.currentId) {
      state.data = await api(`/api/samples/${state.currentId}`);
      state.locations = await api("/api/locations");
      renderAll();
    }
  }

  // ---------- 样本新建 / 删除 ----------

  const modal = $("#modal-mask");
  $("#btn-new-sample").addEventListener("click", () => {
    $("#sample-form").reset();
    modal.hidden = false;
    $("#s-code").focus();
  });
  $("#btn-cancel-sample").addEventListener("click", () => (modal.hidden = true));
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.hidden = true;
  });

  $("#sample-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      code: $("#s-code").value.trim(),
      name: $("#s-name").value.trim(),
      description: $("#s-desc").value.trim(),
    };
    try {
      const created = await api("/api/samples", { method: "POST", body: JSON.stringify(payload) });
      modal.hidden = true;
      toast("样本已创建");
      await loadSamples(created.id);
    } catch (err) {
      toast(err.message, true);
    }
  });

  $("#btn-delete-sample").addEventListener("click", async () => {
    const { sample } = state.data;
    const total = (state.data.attachments || []).length;
    if (total > 0) {
      toast(`该样本下还有 ${total} 个事件证据附件，请先逐事件处理（删除）附件后再删除样本`, true);
      return;
    }
    if (!confirm(`确定删除样本 ${sample.code} 及其全部交接事件？此操作不可恢复。`)) return;
    try {
      await api(`/api/samples/${state.currentId}`, { method: "DELETE" });
      toast("样本已删除");
      state.currentId = null;
      await loadSamples();
      showEmpty();
    } catch (err) {
      toast(err.message, true);
    }
  });

  // ---------- 结论签署 ----------

  function renderSignoffs() {
    const signoffs = state.data.signoffs || [];
    // 签署时间默认填当前时间（不覆盖用户正在输入的值）
    if (!$("#g-signed-at").value) $("#g-signed-at").value = nowLocalInput();

    const tbody = $("#signoff-tbody");
    if (!signoffs.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="muted-cell" style="text-align:center;padding:18px">暂无签署记录，可在上方表单签署当前审计结论</td></tr>`;
      return;
    }
    tbody.innerHTML = signoffs
      .map((s, i) => {
        const badge =
          s.status === "active"
            ? `<span class="badge badge-pass">已签署</span>`
            : `<span class="badge badge-fail">已撤销</span>`;
        const revokeInfo =
          s.status === "revoked"
            ? `<div class="muted-cell">原因：${esc(s.revoke_reason || "—")}</div>`
            : "";
        const sum = s.summary || {};
        const brief =
          `${STATUS_LABEL[s.audit_status] || s.audit_status || "—"} · ` +
          `${s.event_count} 条事件 · 断档 ${sum.gaps ?? 0} · 重叠 ${sum.overlaps ?? 0}`;
        return `
        <tr>
          <td class="col-ord">${signoffs.length - i}</td>
          <td>${esc(s.signer)}${s.remark ? `<div class="muted-cell">${esc(s.remark)}</div>` : ""}</td>
          <td>${esc(s.signed_at)}</td>
          <td>${esc(brief)}</td>
          <td>${badge}${revokeInfo}</td>
          <td><code class="digest">${esc(s.digest)}</code></td>
          <td><button class="link-btn btn-edit" data-signoff="${s.id}">详情 / 撤销</button></td>
        </tr>`;
      })
      .join("");
  }

  $("#signoff-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      signer: $("#g-signer").value.trim(),
      signed_at: $("#g-signed-at").value,
      remark: $("#g-remark").value.trim(),
    };
    if (!payload.signer || !payload.signed_at) {
      toast("请填写签署人与签署时间", true);
      return;
    }
    try {
      await api(`/api/samples/${state.currentId}/signoffs`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast("已签署，快照已固化保存");
      $("#signoff-form").reset();
      $("#g-signed-at").value = nowLocalInput();
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
  });

  // 打开快照详情弹窗
  $("#signoff-tbody").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-signoff]");
    if (!btn) return;
    try {
      const detail = await api(`/api/signoffs/${btn.dataset.signoff}`);
      state.viewSignoffId = detail.id;
      renderSignoffDetail(detail);
      $("#signoff-modal").hidden = false;
    } catch (err) {
      toast(err.message, true);
    }
  });

  function renderSignoffDetail(d) {
    const snap = d.snapshot || {};
    const sample = snap.sample || {};
    const audit = snap.audit || {};
    const events = audit.events || [];
    const findings = audit.findings || [];
    const sum = audit.summary || {};

    const statusBadge =
      d.status === "active"
        ? `<span class="badge badge-pass">已签署</span>`
        : `<span class="badge badge-fail">已撤销</span>`;
    const integrity = d.integrity_ok
      ? `<span class="integrity-ok">✓ 快照完整，未被篡改</span>`
      : `<span class="integrity-bad">✗ 快照校验不一致！</span>`;

    const findingItems = findings.length
      ? findings
          .map(
            (f) => `
          <li class="finding-${f.code}">
            <span class="tag">${FINDING_LABEL[f.code] || f.code}</span>
            <span>${esc(f.message)}</span>
          </li>`
          )
          .join("")
      : `<li class="muted-item">✓ 签署时未发现断档、重叠或逆序问题。</li>`;

    const eventRows = events.length
      ? events
          .map(
            (e, i) => `
          <tr class="${e.has_conflict ? "row-conflict" : ""}">
            <td class="col-ord">${i + 1}</td>
            <td class="col-seq">${e.seq}</td>
            <td>${e.has_conflict ? '<span class="conflict-flag">⚠</span>' : ""}${esc(e.custodian)}</td>
            <td>${esc(e.start_time)}</td>
            <td>${e.open ? '<span class="open-tag">仍在保管</span>' : esc(e.end_time)}</td>
            <td>${esc(e.duration)}</td>
            <td>${esc(e.note || "—")}</td>
          </tr>`
          )
          .join("")
      : `<tr><td colspan="7" class="muted-cell" style="text-align:center">签署时暂无交接事件</td></tr>`;

    $("#signoff-detail").innerHTML = `
      <div class="signoff-meta">
        <div><span class="meta-lbl">状态</span>${statusBadge}</div>
        <div><span class="meta-lbl">签署人</span>${esc(d.signer)}</div>
        <div><span class="meta-lbl">签署时间</span>${esc(d.signed_at)}</div>
        <div><span class="meta-lbl">备注</span>${esc(d.remark || "—")}</div>
        <div><span class="meta-lbl">样本</span>${esc(sample.code || "")} · ${esc(sample.name || "")}</div>
        <div><span class="meta-lbl">快照校验值</span><code class="digest">${esc(d.digest)}</code> ${integrity}</div>
      </div>
      <div class="signoff-section">
        <h4>签署时审计结论（${STATUS_LABEL[audit.status] || audit.status || "—"}）</h4>
        <p class="conclusion">${esc(audit.conclusion || "")}</p>
        <div class="stats">
          <div class="stat"><div class="num">${sum.total ?? 0}</div><div class="lbl">交接记录</div></div>
          <div class="stat ${sum.gaps ? "bad" : ""}"><div class="num">${sum.gaps ?? 0}</div><div class="lbl">断档</div></div>
          <div class="stat ${sum.overlaps ? "bad" : ""}"><div class="num">${sum.overlaps ?? 0}</div><div class="lbl">重叠</div></div>
          <div class="stat ${sum.reversed ? "bad" : ""}"><div class="num">${sum.reversed ?? 0}</div><div class="lbl">起止倒置</div></div>
          <div class="stat ${sum.out_of_order ? "warn" : ""}"><div class="num">${sum.out_of_order ?? 0}</div><div class="lbl">逆序录入</div></div>
        </div>
        <ul class="findings">${findingItems}</ul>
      </div>
      <div class="signoff-section">
        <h4>签署时交接事件（共 ${events.length} 条，快照内容不随后续变更改写）</h4>
        <div class="table-wrap">
          <table class="event-table">
            <thead>
              <tr><th>排序#</th><th>录入序</th><th>经手人</th><th>起始时间</th><th>结束时间</th><th>时长</th><th>备注</th></tr>
            </thead>
            <tbody>${eventRows}</tbody>
          </table>
        </div>
      </div>`;

    // 撤销区：仅有效签署可撤销；已撤销的展示原因
    const active = d.status === "active";
    $("#revoke-zone").hidden = !active;
    $("#revoked-info").hidden = active;
    if (active) {
      $("#revoke-reason").value = "";
    } else {
      $("#revoked-info").innerHTML =
        `⚠ 该签署已于 <b>${esc(d.revoked_at || "")}</b> 撤销，原因：${esc(d.revoke_reason || "—")}。` +
        `原快照仍完整保留，仅供追溯。`;
    }
  }

  const signoffModal = $("#signoff-modal");
  $("#btn-close-signoff").addEventListener("click", () => (signoffModal.hidden = true));
  signoffModal.addEventListener("click", (e) => {
    if (e.target === signoffModal) signoffModal.hidden = true;
  });

  $("#btn-revoke").addEventListener("click", async () => {
    const reason = $("#revoke-reason").value.trim();
    if (!reason) {
      toast("请填写撤销原因", true);
      return;
    }
    if (!confirm("确定撤销该签署？撤销后原快照保留，但状态将标记为已撤销。")) return;
    try {
      await api(`/api/signoffs/${state.viewSignoffId}/revoke`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      toast("签署已撤销，原快照已保留");
      const detail = await api(`/api/signoffs/${state.viewSignoffId}`);
      renderSignoffDetail(detail);
      await refresh();
    } catch (err) {
      toast(err.message, true);
    }
  });

  // ---------- 样本分装谱系 ----------

  const linState = { rows: [] }; // 分装表单的子样本行 [{ code, name, quantity }]

  function curSample() {
    return (state.data && state.data.sample) || null;
  }

  function renderLineage() {
    const sample = curSample();
    if (!sample) return;
    const ready = sample.initial_quantity != null;
    const unit = sample.unit || "";

    // 当前样本数量状态
    $("#lin-current").innerHTML = ready
      ? [
          statChip(sample.initial_quantity, `初始数量（${esc(unit)}）`, ""),
          statChip(sample.remaining_quantity, `当前剩余（${esc(unit)}）`, "ok"),
          statChip(sample.version, "样本版本", "muted"),
          sample.parent_sample_id
            ? `<span class="ho-stat warn"><b>谱系来源</b> 子样本（由批次分装产生）</span>`
            : `<span class="ho-stat muted"><b>谱系来源</b> 根样本（最初父样本）</span>`,
        ].join("")
      : [
          statChip("未设置", "初始数量", "bad"),
          statChip(sample.version, "样本版本", "muted"),
          `<span class="ho-stat warn"><b>首次分装前</b> 请先设置初始数量与单位</span>`,
        ].join("");

    // 首次分装前的数量初始化表单（仅在未设置时可见）
    $("#qty-form").hidden = ready;
    if (!ready) {
      $("#qty-form").reset();
      $(".lin-version-hint").textContent =
        `提交将基于当前样本版本 v${sample.version}（乐观锁）；` +
        "若期间样本被他人改动，服务端返回 409 并刷新页面。";
    }

    // 分装表单（已设置数量才可见；余量为 0 时禁用提交）
    const form = $("#aliquot-form");
    form.hidden = !ready;
    if (ready) {
      $("#al-available").textContent = `${sample.remaining_quantity} ${unit}`;
      if (!linState.rows.length) addAliquotRow();
      renderAliquotRows();
    }

    renderLineageTree();
  }

  // ---- 分装表单：子样本动态行 ----

  function addAliquotRow(code = "", name = "", quantity = "") {
    linState.rows.push({ code, name, quantity });
  }

  function renderAliquotRows() {
    const wrap = $("#al-rows");
    if (!linState.rows.length) {
      wrap.innerHTML = `<div class="muted-cell" style="padding:8px 0">请点击「＋ 添加子样本」至少填写一个子样本</div>`;
    } else {
      wrap.innerHTML = linState.rows
        .map(
          (r, i) => `
        <div class="al-row" data-idx="${i}">
          <input type="text" class="al-code" maxlength="64" placeholder="子样本编号（唯一）" value="${esc(r.code)}" />
          <input type="text" class="al-name" maxlength="128" placeholder="名称（可选，默认同编号）" value="${esc(r.name)}" />
          <input type="text" class="al-qty" maxlength="40" inputmode="decimal" placeholder="分装量" value="${esc(r.quantity)}" />
          <button type="button" class="link-btn btn-del al-del" data-del="${i}">移除</button>
        </div>`
        )
        .join("");
    }
    updateAliquotSum();
  }

  function syncAliquotRows() {
    $("#al-rows")
      .querySelectorAll(".al-row")
      .forEach((el) => {
        const i = Number(el.dataset.idx);
        linState.rows[i].code = el.querySelector(".al-code").value.trim();
        linState.rows[i].name = el.querySelector(".al-name").value.trim();
        linState.rows[i].quantity = el.querySelector(".al-qty").value.trim();
      });
  }

  function updateAliquotSum() {
    // 前端只做形态提示；权威的精确十进制合计 / 余量比较在服务端完成
    const filled = linState.rows.filter((r) => r.quantity.trim()).length;
    const bad = linState.rows.some(
      (r) => r.quantity.trim() && !/^\d+(\.\d+)?$/.test(r.quantity.trim())
    );
    $("#al-sum").innerHTML = bad
      ? `<span class="ho-stat bad"><b>合计</b> 存在无法识别的分装量，请输入正十进制（如 30、0.25）</span>`
      : `<span class="ho-stat ${filled ? "" : "muted"}"><b>已填 ${filled} 行</b> 合计与余量由服务端按精确十进制校验</span>`;
  }

  $("#btn-add-al-row").addEventListener("click", () => {
    syncAliquotRows();
    addAliquotRow();
    renderAliquotRows();
    const inputs = $("#al-rows").querySelectorAll(".al-code");
    inputs[inputs.length - 1].focus();
  });

  $("#al-rows").addEventListener("input", () => {
    syncAliquotRows();
    updateAliquotSum();
  });

  $("#al-rows").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-del]");
    if (!btn) return;
    syncAliquotRows();
    linState.rows.splice(Number(btn.dataset.del), 1);
    renderAliquotRows();
  });

  // ---- 设置初始数量 / 单位 ----

  $("#qty-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const sample = curSample();
    const payload = {
      initial_quantity: $("#q-amount").value.trim(),
      unit: $("#q-unit").value.trim(),
      expected_version: sample.version,
    };
    if (!payload.initial_quantity || !payload.unit) {
      toast("请填写初始数量与单位", true);
      return;
    }
    if (!/^\d+(\.\d+)?$/.test(payload.initial_quantity)) {
      toast("初始数量必须是正十进制（如 100、0.25），且不要使用科学计数法", true);
      return;
    }
    try {
      const r = await api(`/api/samples/${sample.id}/quantity`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      toast(`已设置初始数量 ${r.initial_quantity} ${r.unit}（v${r.version}）`);
      await reloadCurrentSample(sample.id);
      linState.rows = [];
    } catch (err) {
      toast(err.message, true);
      if (err.status === 409) await reloadCurrentSample(sample.id);
    }
  });

  // ---- 执行分装 ----

  $("#aliquot-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const sample = curSample();
    syncAliquotRows();
    const rows = linState.rows.filter(
      (r) => r.code.trim() || r.name.trim() || r.quantity.trim()
    );
    if (!rows.length) {
      toast("请至少添加一个子样本", true);
      return;
    }
    for (const r of rows) {
      if (!r.code.trim()) return toast("每个子样本都必须填写编号", true);
      if (!r.quantity.trim()) return toast(`子样本 ${r.code} 的分装量不能为空`, true);
      if (!/^\d+(\.\d+)?$/.test(r.quantity.trim()))
        return toast(`子样本 ${r.code} 的分装量必须是正十进制`, true);
    }
    const operator = $("#al-operator").value.trim();
    if (!operator) return toast("请填写操作人", true);

    const payload = {
      operator,
      aliquot_at: $("#al-at").value || null,
      note: $("#al-note").value.trim(),
      expected_version: sample.version,
      items: rows.map((r) => ({
        code: r.code.trim(),
        name: r.name.trim() || null,
        quantity: r.quantity.trim(),
      })),
    };
    if (
      !confirm(
        `确认从 ${sample.code} 分装 ${rows.length} 个子样本？\n` +
          "服务端将在单一事务内校验编号唯一、单位一致、分装量为正且合计不超过余量；\n" +
          "成功后子样本创建、余量扣减与不可变批次一并落库。"
      )
    )
      return;
    const btn = $("#aliquot-form button[type=submit]");
    btn.disabled = true;
    try {
      const r = await api(`/api/samples/${sample.id}/aliquots`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.data.sample = r.parent;
      state.data.lineage = r.lineage;
      renderAll();
      toast(
        `分装批次 ${r.batch_no} 完成：${r.items.length} 个子样本，合计 ${r.total_quantity} ${r.unit}，剩余 ${r.remaining_after}`
      );
      $("#al-operator").value = operator; // 保留操作人便于连续操作
      $("#al-note").value = "";
      $("#al-at").value = "";
      linState.rows = [];
      await loadSamples(state.currentId); // 左侧列表出现新子样本
      renderAll();
    } catch (err) {
      toast(err.message, true);
      // 重复提交 / 并发变更（409）：以服务端最新状态刷新，避免再次重复提交
      if (err.status === 409 || err.status === 404) {
        await reloadCurrentSample(sample.id);
      }
    } finally {
      btn.disabled = false;
    }
  });

  async function reloadCurrentSample(id) {
    state.data = await api(`/api/samples/${id}`);
    state.locations = await api("/api/locations");
    linState.rows = [];
    renderAll();
    await loadSamples(id);
  }

  // ---- 父子树渲染 ----

  function renderLineageTree() {
    const wrap = $("#lin-tree");
    const lineage = state.data.lineage;
    if (!lineage) {
      wrap.innerHTML = `<div class="muted-cell">暂无谱系数据</div>`;
      return;
    }
    wrap.innerHTML = renderLinNode(lineage.tree, 0);
  }

  function renderLinNode(node, depth) {
    const isCurrent = node.id === state.data.sample.id;
    const ready = node.initial_quantity != null;
    const qty = ready
      ? `初始 <span class="lin-qty-val">${esc(node.initial_quantity)}</span> · 剩余 <b>${esc(node.remaining_quantity)}</b> ${esc(node.unit)}`
      : '<span class="muted-cell">未设置数量</span>';
    const via = node.via_batch
      ? `<div class="lin-via">来源批次
           <a href="javascript:void(0)" data-batch="${node.via_batch.batch_id}" class="lin-batch-link">${esc(node.via_batch.batch_no)}</a>
           · 分装量 <b>${esc(node.via_batch.aliquot_quantity)}</b> ${esc(node.via_batch.unit)}
           · ${esc(node.via_batch.aliquot_at || "")} · ${esc(node.via_batch.operator || "")}
         </div>`
      : `<div class="lin-via muted-cell">根样本（无上游来源）</div>`;
    const goBtn = isCurrent
      ? `<span class="badge badge-pass">当前样本</span>`
      : `<button class="link-btn btn-edit" data-go="${node.id}">进入并继续分装</button>`;
    const children = node.children.length
      ? `<div class="lin-children">${node.children.map((c) => renderLinNode(c, depth + 1)).join("")}</div>`
      : "";
    return `
    <div class="lin-node ${isCurrent ? "current" : ""}">
      <div class="lin-node-main">
        <div class="lin-node-info">
          <span class="s-code">${esc(node.code)}</span>
          <span class="lin-name">${esc(node.name)}</span>
          ${goBtn}
        </div>
        <div class="lin-qty">${qty}</div>
        ${via}
      </div>
      ${children}
    </div>`;
  }

  $("#lin-tree").addEventListener("click", async (e) => {
    const batchLink = e.target.closest("a[data-batch]");
    if (batchLink) {
      openBatchDetail(Number(batchLink.dataset.batch));
      return;
    }
    const goBtn = e.target.closest("button[data-go]");
    if (goBtn) {
      await selectSample(Number(goBtn.dataset.go));
      document.querySelector("#lin-current")?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });

  // ---- 批次详情弹窗 ----

  async function openBatchDetail(batchId) {
    try {
      const d = await api(`/api/aliquot-batches/${batchId}`);
      const integrity = d.integrity_ok
        ? `<span class="integrity-ok">✓ 快照完整，SHA-256 校验一致（批次不可变）</span>`
        : `<span class="integrity-bad">✗ 批次快照校验不一致！</span>`;
      const parentInfo = d.parent
        ? `${esc(d.parent.code)} · ${esc(d.parent.name)}（当前剩余 ${esc(d.parent.remaining_quantity)} ${esc(d.parent.unit)}）`
        : `<span class="muted-cell">父样本已不存在</span>`;
      $("#batch-detail-body").innerHTML = `
        <div class="signoff-meta">
          <div><span class="meta-lbl">批次编号</span><b>${esc(d.batch_no)}</b></div>
          <div><span class="meta-lbl">父样本</span>${parentInfo}</div>
          <div><span class="meta-lbl">操作人</span>${esc(d.operator)}</div>
          <div><span class="meta-lbl">分装时间</span>${esc(d.aliquot_at)}</div>
          <div><span class="meta-lbl">创建时间</span>${esc(d.created_at)}</div>
          <div><span class="meta-lbl">父样本版本</span>提交基于 v${d.parent_version_before}</div>
          <div><span class="meta-lbl">单位</span>${esc(d.unit)}</div>
          <div><span class="meta-lbl">余量变化</span>${esc(d.remaining_before)} → <b>${esc(d.remaining_after)}</b> ${esc(d.unit)}</div>
          <div><span class="meta-lbl">分装合计</span><b>${esc(d.total_quantity)}</b> ${esc(d.unit)}（${d.item_count} 个子样本）</div>
          <div><span class="meta-lbl">备注</span>${esc(d.note || "—")}</div>
          <div><span class="meta-lbl">摘要校验</span>${integrity}</div>
          <div><span class="meta-lbl">SHA-256</span><code class="digest">${esc(d.digest)}</code></div>
        </div>
        <div class="signoff-section">
          <h4>分装明细（不可变）</h4>
          <div class="table-wrap">
            <table class="event-table">
              <thead><tr><th>#</th><th>子样本编号</th><th>名称</th><th>分装量</th><th>子样本当前剩余</th><th>操作</th></tr></thead>
              <tbody>
                ${d.items
                  .map(
                    (it) => `
                  <tr>
                    <td class="col-ord">${it.seq}</td>
                    <td><span class="s-code">${esc(it.child_code)}</span></td>
                    <td>${esc(it.child_name || "—")}</td>
                    <td><b>${esc(it.quantity)}</b> ${esc(it.unit)}</td>
                    <td>${
                      it.child_remaining_quantity == null
                        ? '<span class="muted-cell">样本已删除</span>'
                        : esc(it.child_remaining_quantity) + " " + esc(it.unit)
                    }</td>
                    <td>${
                      it.child_sample_id
                        ? `<button class="link-btn btn-edit" data-batch-go="${it.child_sample_id}">进入子样本</button>`
                        : ""
                    }</td>
                  </tr>`
                  )
                  .join("")}
              </tbody>
            </table>
          </div>
        </div>`;
      $("#batch-modal").hidden = false;
    } catch (err) {
      toast(err.message, true);
    }
  }

  $("#batch-detail-body").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-batch-go]");
    if (btn) {
      $("#batch-modal").hidden = true;
      await selectSample(Number(btn.dataset.batchGo));
      document.querySelector("#lin-current")?.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });

  const batchModal = $("#batch-modal");
  $("#btn-close-batch").addEventListener("click", () => (batchModal.hidden = true));
  batchModal.addEventListener("click", (e) => {
    if (e.target === batchModal) batchModal.hidden = true;
  });

  // ---------- 样本库位管理 ----------

  $("#btn-locations").addEventListener("click", () => {
    setView("locations");
    loadLocationsView();
  });
  $("#btn-loc-refresh").addEventListener("click", loadLocationsView);

  async function loadLocationsView() {
    try {
      state.locations = await api("/api/locations");
      renderLocationsView();
    } catch (err) {
      toast(err.message, true);
    }
  }

  function renderLocationsView() {
    const locs = state.locations || [];
    const totalCap = locs.reduce((a, l) => a + l.capacity, 0);
    const totalOcc = locs.reduce((a, l) => a + l.occupied, 0);
    $("#loc-stats").innerHTML = [
      statChip(locs.length, "库位", ""),
      statChip(totalCap, "总容量", ""),
      statChip(totalOcc, "已占用", totalOcc ? "warn" : "muted"),
      statChip(totalCap - totalOcc, "剩余空位", "ok"),
    ].join("");

    const tbody = $("#loc-tbody");
    if (!locs.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted-cell" style="text-align:center;padding:22px">暂无库位，请在上方创建（编号唯一、容量为正整数）</td></tr>`;
      return;
    }
    tbody.innerHTML = locs
      .map((l) => {
        const pct = l.capacity ? Math.round((l.occupied / l.capacity) * 100) : 0;
        const full = l.remaining <= 0;
        return `
        <tr>
          <td><span class="s-code">${esc(l.code)}</span></td>
          <td>${esc(l.name)}</td>
          <td>${l.capacity}</td>
          <td>${l.occupied}</td>
          <td>${full ? '<span class="badge badge-fail">已满</span>' : l.remaining}</td>
          <td><span class="loc-bar${full ? " full" : ""}"><i style="width:${pct}%"></i></span><span class="muted-cell">${pct}%</span></td>
          <td class="muted-cell">v${l.version}</td>
          <td><button class="link-btn btn-edit" data-loc-view="${l.id}">占用清单</button></td>
        </tr>`;
      })
      .join("");
  }

  // 创建库位
  $("#location-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      code: $("#loc-code").value.trim(),
      name: $("#loc-name").value.trim(),
      capacity: Number($("#loc-capacity").value),
    };
    if (!payload.code || !payload.name) {
      toast("请填写库位编号与名称", true);
      return;
    }
    if (!Number.isInteger(payload.capacity) || payload.capacity < 1) {
      toast("容量必须是正整数", true);
      return;
    }
    try {
      const loc = await api("/api/locations", { method: "POST", body: JSON.stringify(payload) });
      toast(`库位 ${loc.code} 已创建（容量 ${loc.capacity}）`);
      $("#location-form").reset();
      await loadLocationsView();
    } catch (err) {
      toast(err.message, true);
      if (err.status === 409) await loadLocationsView(); // 编号冲突：刷新列表
    }
  });

  // 库位详情弹窗：占用清单 + 移动历史
  $("#loc-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-loc-view]");
    if (btn) openLocationDetail(Number(btn.dataset.locView));
  });

  async function openLocationDetail(id) {
    try {
      const d = await api(`/api/locations/${id}`);
      const pct = d.capacity ? Math.round((d.occupied / d.capacity) * 100) : 0;
      const occRows = d.occupants.length
        ? d.occupants
            .map(
              (o, i) => `
          <tr>
            <td class="col-ord">${i + 1}</td>
            <td><span class="s-code">${esc(o.code)}</span></td>
            <td>${esc(o.name)}</td>
            <td class="muted-cell">${esc(o.placed_at)}</td>
            <td><button class="link-btn btn-edit" data-loc-go="${o.sample_id}">进入样本</button></td>
          </tr>`
            )
            .join("")
        : `<tr><td colspan="5" class="muted-cell" style="text-align:center;padding:16px">库位当前空闲</td></tr>`;
      const moveRows = d.moves.length
        ? d.moves
            .map(
              (m) => `
          <tr>
            <td>${esc(m.moved_at)}</td>
            <td><span class="s-code">${esc(m.sample_code)}</span></td>
            <td>${m.from_code ? esc(m.from_code) : '<span class="muted-cell">（未入库）</span>'} → ${
              m.to_code ? esc(m.to_code) : '<span class="muted-cell">（移出库位）</span>'
            }</td>
            <td>${esc(m.operator)}</td>
            <td>${esc(m.note || "—")}</td>
          </tr>`
            )
            .join("")
        : `<tr><td colspan="5" class="muted-cell" style="text-align:center;padding:16px">暂无移动记录</td></tr>`;
      $("#location-detail-body").innerHTML = `
        <div class="signoff-meta">
          <div><span class="meta-lbl">库位编号</span><b>${esc(d.code)}</b></div>
          <div><span class="meta-lbl">名称</span>${esc(d.name)}</div>
          <div><span class="meta-lbl">占用</span><b>${d.occupied}</b> / ${d.capacity}（剩余 ${d.remaining}）
            <span class="loc-bar${d.remaining <= 0 ? " full" : ""}"><i style="width:${pct}%"></i></span>${pct}%</div>
          <div><span class="meta-lbl">版本</span>v${d.version}</div>
          <div><span class="meta-lbl">创建时间</span>${esc(d.created_at)}</div>
        </div>
        <div class="signoff-section">
          <h4>当前占用清单（${d.occupants.length} 个样本）</h4>
          <div class="table-wrap">
            <table class="event-table">
              <thead><tr><th>#</th><th>样本编号</th><th>名称</th><th>放入时间</th><th>操作</th></tr></thead>
              <tbody>${occRows}</tbody>
            </table>
          </div>
        </div>
        <div class="signoff-section">
          <h4>移动历史（不可变记录，最近 ${d.moves.length} 条）</h4>
          <div class="table-wrap">
            <table class="event-table">
              <thead><tr><th>移动时间</th><th>样本</th><th>从 → 到</th><th>操作人</th><th>备注</th></tr></thead>
              <tbody>${moveRows}</tbody>
            </table>
          </div>
        </div>`;
      $("#location-modal").hidden = false;
    } catch (err) {
      toast(err.message, true);
    }
  }

  // 从库位详情直接进入样本
  $("#location-detail-body").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-loc-go]");
    if (!btn) return;
    $("#location-modal").hidden = true;
    await selectSample(Number(btn.dataset.locGo));
  });

  const locationModal = $("#location-modal");
  $("#btn-close-location").addEventListener("click", () => (locationModal.hidden = true));
  locationModal.addEventListener("click", (e) => {
    if (e.target === locationModal) locationModal.hidden = true;
  });

  // ---- 样本详情页：库位卡片 ----

  function renderLocationCard() {
    const sample = curSample();
    if (!sample) return;
    const cur = state.data.location || null;
    const locs = state.locations || [];

    // 当前位置
    $("#loc-current").innerHTML = cur
      ? [
          statChip(esc(cur.code), `当前库位 · ${esc(cur.name)}`, "ok"),
          statChip(`v${sample.version}`, "样本版本", "muted"),
          `<span class="ho-stat muted"><b>放入时间</b> ${esc(cur.placed_at)}</span>`,
        ].join("")
      : [
          statChip("未入库", "当前位置", "warn"),
          statChip(`v${sample.version}`, "样本版本", "muted"),
          `<span class="ho-stat muted"><b>提示</b> 选择目标库位并填写操作人即可放入</span>`,
        ].join("");

    // 目标库位下拉：排除当前所在库位；已满库位禁用
    const sel = $("#mv-target");
    const candidates = locs.filter((l) => !cur || l.id !== cur.location_id);
    sel.innerHTML = candidates.length
      ? candidates
          .map(
            (l) =>
              `<option value="${l.id}" ${l.remaining <= 0 ? "disabled" : ""}>` +
              `${esc(l.code)} · ${esc(l.name)}（剩余 ${l.remaining}/${l.capacity}）</option>`
          )
          .join("")
      : `<option value="">（暂无可用库位，请先在「库位管理」中创建）</option>`;
    const hasTarget = candidates.some((l) => l.remaining > 0);
    $("#mv-submit").disabled = !hasTarget;
    $("#mv-submit").textContent = cur ? "转移到目标库位" : "放入目标库位";
    $("#mv-target-hint").textContent = hasTarget
      ? "仅列出有空位的库位（不含当前所在库位）；提交携带样本与库位版本做乐观锁校验"
      : "当前没有有空位的库位，请先在左侧「样本库位管理」中创建";
    $("#mv-checkout").hidden = !cur;

    // 移动历史（不可变记录，新的在前）
    const moves = state.data.location_moves || [];
    $("#mv-history-tbody").innerHTML = moves.length
      ? moves
          .map(
            (m, i) => `
        <tr>
          <td class="col-ord">${moves.length - i}</td>
          <td>${esc(m.moved_at)}</td>
          <td>${m.from_code ? esc(m.from_code) : '<span class="muted-cell">（未入库）</span>'}</td>
          <td>${m.to_code ? esc(m.to_code) : '<span class="muted-cell">（移出库位）</span>'}</td>
          <td>${esc(m.operator)}</td>
          <td>${esc(m.note || "—")}</td>
        </tr>`
          )
          .join("")
      : `<tr><td colspan="6" class="muted-cell" style="text-align:center;padding:18px">暂无移动记录</td></tr>`;
  }

  async function submitMove(payload, okMsg) {
    const btn = $("#mv-submit");
    btn.disabled = true;
    try {
      const r = await api(`/api/samples/${state.currentId}/location-move`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      // 服务端回传刷新后的样本 / 当前位置 / 移动历史 / 库位列表，直接渲染
      state.data.sample = r.sample;
      state.data.location = r.location;
      state.data.location_moves = r.location_moves;
      state.locations = r.locations;
      renderAll();
      toast(okMsg);
    } catch (err) {
      toast(err.message, true);
      // 并发争抢 / 陈旧来源 / 版本过期（409）：以服务端最新状态刷新后由用户重试
      if (err.status === 409 || err.status === 404) {
        await reloadCurrentSample(state.currentId);
      }
    } finally {
      btn.disabled = false;
    }
  }

  function movePayload(toLoc) {
    const sample = curSample();
    const cur = state.data.location || null;
    return {
      to_location_id: toLoc ? toLoc.id : null,
      operator: $("#mv-operator").value.trim(),
      note: $("#mv-note").value.trim(),
      expected_sample_version: sample.version,
      expected_from_location_id: cur ? cur.location_id : null,
      expected_from_version: cur ? cur.location_version : null,
      expected_to_version: toLoc ? toLoc.version : null,
    };
  }

  // 放入 / 转移
  $("#move-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const cur = state.data.location || null;
    const targetId = Number($("#mv-target").value);
    const toLoc = (state.locations || []).find((l) => l.id === targetId);
    if (!toLoc) {
      toast("请选择目标库位", true);
      return;
    }
    if (!$("#mv-operator").value.trim()) {
      toast("请填写操作人", true);
      return;
    }
    await submitMove(
      movePayload(toLoc),
      `已${cur ? "转移" : "放入"}到库位 ${toLoc.code}（${toLoc.name}）`
    );
  });

  // 移出库位（不进入新库位）
  $("#mv-checkout").addEventListener("click", async () => {
    const cur = state.data.location || null;
    if (!cur) return;
    if (!$("#mv-operator").value.trim()) {
      toast("请填写操作人", true);
      return;
    }
    if (!confirm(`确定将样本从库位 ${cur.code}（${cur.name}）移出？移出后样本不占任何库位。`))
      return;
    await submitMove(movePayload(null), `已移出库位 ${cur.code}`);
  });

  // ---------- 报告导出 ----------

  document.querySelectorAll("[data-export]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const fmt = btn.dataset.export;
      const url = `/api/samples/${state.currentId}/report?format=${fmt}`;
      const a = document.createElement("a");
      a.href = url;
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
  });

  // ---------- 批量导入交接事件 ----------

  const importState = { token: null, preview: null, fileName: "" };

  const AUDIT_STATUS_LABEL = STATUS_LABEL;

  function statPill(num, label, cls = "") {
    return `<span class="ho-stat ${cls}"><b>${num}</b> ${label}</span>`;
  }

  function openImportModal() {
    $("#import-file").value = "";
    $("#import-summary").hidden = true;
    $("#import-preview-wrap").hidden = true;
    $("#import-audit-change").hidden = true;
    $("#import-result").hidden = true;
    $("#btn-confirm-import").disabled = true;
    importState.token = null;
    importState.preview = null;
    $("#import-modal").hidden = false;
  }

  $("#btn-bulk-import").addEventListener("click", openImportModal);

  $("#btn-download-template").addEventListener("click", () => {
    const a = document.createElement("a");
    a.href = `/api/samples/${state.currentId}/events/import-template`;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
  });

  const importModal = $("#import-modal");
  $("#btn-close-import").addEventListener("click", () => (importModal.hidden = true));
  importModal.addEventListener("click", (e) => {
    if (e.target === importModal) importModal.hidden = true;
  });

  $("#btn-preview-import").addEventListener("click", async () => {
    const fileInput = $("#import-file");
    const file = fileInput.files && fileInput.files[0];
    if (!file) {
      toast("请先选择要导入的 UTF-8 CSV 文件", true);
      return;
    }
    try {
      const buf = await file.arrayBuffer();
      const resp = await fetch(
        `/api/samples/${state.currentId}/events/import-preview`,
        { method: "POST", body: buf }
      );
      const payload = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(extractError(payload) || `预览失败（${resp.status}）`);
      importState.preview = payload;
      importState.token = payload.token;
      importState.fileName = file.name;
      renderImportPreview(payload);
    } catch (err) {
      toast(err.message, true);
    }
  });

  function renderImportPreview(p) {
    // 汇总条
    const sumEl = $("#import-summary");
    const okCls = p.all_valid ? "ok" : "bad";
    sumEl.className = "import-summary " + (p.all_valid ? "import-ok" : "import-bad");
    sumEl.hidden = false;
    sumEl.innerHTML = `
      ${statPill(p.total_rows, "数据行", "")}
      ${statPill(p.valid_rows, "校验通过", p.all_valid && p.valid_rows ? "ok" : "muted")}
      ${statPill(p.invalid_rows, "校验错误", p.invalid_rows ? "bad" : "muted")}
      <span class="import-sum-msg">
        ${p.all_valid
          ? `全部行校验通过，录入序将按文件顺序分配为 <b>${p.rows[0].seq_preview} ~ ${p.rows[p.rows.length - 1].seq_preview}</b>`
          : "存在校验错误的行，无法导入；请修正文件后重新预览（预览不会写库）"}
      </span>`;

    // 逐行表格
    $("#import-preview-wrap").hidden = false;
    $("#import-preview-tbody").innerHTML = p.rows
      .map((r) => {
        const end = r.end_time ? esc(r.end_time) : '<span class="open-tag">仍在保管（留空）</span>';
        const result = r.errors.length
          ? `<ul class="import-err-list">${r.errors.map((m) => `<li>✗ ${esc(m)}</li>`).join("")}</ul>`
          : `<span class="import-ok-mark">✓ 校验通过</span>${
              r.in_conflict ? `<div class="import-conflict-hint">⚠ 导入后将卷入审计冲突（见下方结论变化）</div>` : ""
            }`;
        return `
        <tr class="${r.errors.length ? "import-row-err" : r.in_conflict ? "import-row-conflict" : ""}">
          <td class="col-ord">${r.line}</td>
          <td class="col-seq">${r.seq_preview ?? "—"}</td>
          <td>${esc(r.custodian || "（空）")}</td>
          <td>${esc(r.start_time || "（空）")}</td>
          <td>${end}</td>
          <td>${esc(r.note || "—")}</td>
          <td>${result}</td>
        </tr>`;
      })
      .join("");

    renderImportAuditChange(p);

    $("#import-result").hidden = true;
    $("#btn-confirm-import").disabled = !p.all_valid;
  }

  function renderImportAuditChange(p) {
    const el = $("#import-audit-change");
    el.hidden = false;
    const cur = p.current.audit;
    const proj = p.projected.audit;
    const curHs = p.current.handover_summary;
    const projHs = p.projected.handover_summary;
    const change = p.projected.handover_change;

    const delta = (a, b) => {
      const d = b - a;
      if (d > 0) return `<span class="import-delta up">+${d}</span>`;
      if (d < 0) return `<span class="import-delta down">${d}</span>`;
      return `<span class="import-delta flat">±0</span>`;
    };

    const findingsHtml = p.projected.new_findings.length
      ? `<div class="import-new-findings">
           <div class="import-sub-lbl">导入后涉及新行的审计发现：</div>
           <ul>${p.projected.new_findings
             .map((f) => `<li class="finding-${f.code}"><span class="tag">${esc(FINDING_LABEL[f.code] || f.code)}</span>${esc(f.message)}</li>`)
             .join("")}</ul>
         </div>`
      : `<div class="import-sub-lbl muted-cell">新行未引入断档 / 重叠 / 倒置发现。</div>`;

    const invalidHtml = change.newly_invalidated
      ? `<div class="import-invalid-warn">⚠ 导入后将有 <b>${change.newly_invalidated}</b> 条原交接确认因相邻关系变化失效（原记录保留，需两侧重新确认）</div>`
      : `<div class="import-sub-lbl muted-cell">现有交接确认不会因本次导入失效。</div>`;

    el.innerHTML = `
      <div class="import-sub-lbl">导入后审计结论变化（预览，不写库）：</div>
      <div class="import-compare">
        <div class="import-side">
          <div class="muted-cell">当前（${p.current.event_count} 条事件）</div>
          <span class="badge badge-${cur.status}">${esc(AUDIT_STATUS_LABEL[cur.status] || cur.status)}</span>
          <p class="import-conclusion">${esc(cur.conclusion)}</p>
          <div class="import-mini-stats">
            断档 ${cur.summary.gaps} · 重叠 ${cur.summary.overlaps} · 倒置 ${cur.summary.reversed} · 逆序 ${cur.summary.out_of_order}
          </div>
          <div class="import-mini-stats">交接确认：已确认 ${curHs.confirmed} / 待确认 ${curHs.pending} / 时间不一致 ${curHs.time_mismatch} / 失效 ${curHs.invalidated}</div>
        </div>
        <div class="import-arrow">➜</div>
        <div class="import-side">
          <div class="muted-cell">导入后（${p.projected.event_count} 条事件）</div>
          <span class="badge badge-${proj.status}">${esc(AUDIT_STATUS_LABEL[proj.status] || proj.status)}</span>
          <p class="import-conclusion">${esc(proj.conclusion)}</p>
          <div class="import-mini-stats">
            断档 ${proj.summary.gaps} ${delta(cur.summary.gaps, proj.summary.gaps)} ·
            重叠 ${proj.summary.overlaps} ${delta(cur.summary.overlaps, proj.summary.overlaps)} ·
            倒置 ${proj.summary.reversed} ${delta(cur.summary.reversed, proj.summary.reversed)} ·
            逆序 ${proj.summary.out_of_order} ${delta(cur.summary.out_of_order, proj.summary.out_of_order)}
          </div>
          <div class="import-mini-stats">交接确认：已确认 ${projHs.confirmed} / 待确认 ${projHs.pending} / 时间不一致 ${projHs.time_mismatch} / 失效 ${projHs.invalidated} ${delta(curHs.invalidated, projHs.invalidated)}</div>
        </div>
      </div>
      ${findingsHtml}
      ${invalidHtml}`;
  }

  $("#btn-confirm-import").addEventListener("click", async () => {
    if (!importState.token) {
      toast("请先完成预览", true);
      return;
    }
    const p = importState.preview;
    if (
      !confirm(
        `确认将 ${p.valid_rows} 行按文件顺序导入（录入序 ${p.rows[0].seq_preview} ~ ${
          p.rows[p.rows.length - 1].seq_preview
        }）？导入在单一事务内完成，任一失败全部回滚。`
      )
    )
      return;
    const btn = $("#btn-confirm-import");
    btn.disabled = true;
    try {
      const resp = await fetch(
        `/api/samples/${state.currentId}/events/import-confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token: importState.token }),
        }
      );
      const payload = await resp.json().catch(() => null);
      if (!resp.ok) {
        btn.disabled = false;
        throw new Error(extractError(payload) || `导入失败（${resp.status}）`);
      }
      renderImportResult(payload);
      importState.token = null; // 令牌一次性使用
      await refresh();
    } catch (err) {
      toast(err.message + "（数据未写入，请重新预览）", true);
      // 链路已变化时旧预览作废
      importState.token = null;
      $("#btn-confirm-import").disabled = true;
    }
  });

  function renderImportResult(res) {
    const el = $("#import-result");
    el.hidden = false;
    el.className = "import-result import-ok";
    const reasonText = {
      event_edited: "关联事件被编辑",
      event_deleted: "关联事件被删除",
      adjacency_changed: "事件不再相邻",
    };
    const invalidDetail = res.invalidated_confirmations
      ? `；另有 <b>${res.invalidated_confirmations}</b> 条原交接确认失效（${Object.entries(
          res.invalidation_reasons
        )
          .map(([k, v]) => `${reasonText[k] || k} ${v} 条`)
          .join("，")}），需两侧重新确认`
      : "";
    el.innerHTML = `
      ✓ 导入完成：成功写入 <b>${res.imported_count}</b> 条交接事件，
      录入序 <b>${res.first_seq} ~ ${res.last_seq}</b>${invalidDetail}。
      <div class="import-mini-stats">导入后审计结论：
        <span class="badge badge-${res.audit.status}">${esc(STATUS_LABEL[res.audit.status] || res.audit.status)}</span>
        ${esc(res.audit.conclusion)}
      </div>`;
    $("#btn-confirm-import").disabled = true;
  }

  // ---------- 事件证据附件 ----------

  const attState = { eventId: null };
  const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
  const MEDIA_LABEL = {
    "application/pdf": "PDF",
    "image/jpeg": "JPEG 图片",
    "image/png": "PNG 图片",
  };

  function fmtBytes(num) {
    const units = ["B", "KiB", "MiB", "GiB"];
    let v = num;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i += 1;
    }
    return i === 0 ? `${num} B` : `${v.toFixed(1)} ${units[i]}`;
  }

  async function openAttachmentModal(eventId) {
    attState.eventId = eventId;
    const ev = state.data.events.find((x) => x.id === eventId);
    $("#att-event-label").textContent = ev
      ? `· 事件 #${eventId}（${ev.custodian}，${ev.start_time}）`
      : `· 事件 #${eventId}`;
    $("#att-file").value = "";
    $("#attachment-modal").hidden = false;
    await renderAttachments(eventId);
  }

  async function renderAttachments(eventId) {
    let list;
    try {
      list = await api(`/api/events/${eventId}/attachments`);
      // 用该事件的最新列表整体替换缓存中的同事件附件，其余事件的保持不变
      const others = (state.data.attachments || []).filter((a) => a.event_id !== eventId);
      state.data.attachments = others.concat(list);
    } catch (err) {
      toast(err.message, true);
      return;
    }
    renderEvents(); // 刷新事件行上的附件数量 / 损坏角标

    const corrupt = list.filter((a) => a.status === "corrupt").length;
    $("#att-summary").innerHTML = [
      statChip(list.length, "附件记录", ""),
      statChip(corrupt, "损坏", corrupt ? "bad" : "muted"),
      statChip(new Set(list.map((a) => a.digest)).size, "去重实体", "ok"),
    ].join("");

    const tbody = $("#att-tbody");
    if (!list.length) {
      tbody.innerHTML = `<tr><td colspan="9" class="muted-cell" style="text-align:center;padding:20px">该事件暂无证据附件，可在上方选择 PDF / JPEG / PNG 上传</td></tr>`;
      return;
    }
    tbody.innerHTML = list
      .map((a, i) => {
        const status =
          a.status === "corrupt"
            ? `<span class="badge badge-fail">损坏</span><div class="muted-cell">${esc(
                a.corruption_reason_text || a.corruption_reason || "—"
              )}</div>`
            : `<span class="badge badge-pass">正常</span><div class="muted-cell">校验于 ${esc(
                a.last_verified_at || "—"
              )}</div>`;
        return `
        <tr class="${a.status === "corrupt" ? "att-row-corrupt" : ""}">
          <td class="col-ord">${i + 1}</td>
          <td>${esc(a.filename)}</td>
          <td>${esc(MEDIA_LABEL[a.media_type] || a.media_type)}</td>
          <td>${esc(a.size_text || fmtBytes(a.size))}</td>
          <td>${esc(a.uploaded_by)}</td>
          <td class="muted-cell">${esc(a.uploaded_at)}</td>
          <td><code class="digest">${esc(a.digest.slice(0, 12))}…</code></td>
          <td>${status}</td>
          <td style="white-space:nowrap">
            <button class="link-btn btn-edit" data-att-dl="${a.id}">下载</button>
            <button class="link-btn btn-edit" data-att-refresh-one="${a.id}">复核</button>
            <button class="link-btn btn-del" data-att-del="${a.id}">删除</button>
          </td>
        </tr>`;
      })
      .join("");
  }

  const attModal = $("#attachment-modal");
  $("#btn-close-att").addEventListener("click", () => (attModal.hidden = true));
  attModal.addEventListener("click", (e) => {
    if (e.target === attModal) attModal.hidden = true;
  });

  $("#btn-att-upload").addEventListener("click", async () => {
    const eventId = attState.eventId;
    const fileInput = $("#att-file");
    const file = fileInput.files && fileInput.files[0];
    if (!file) {
      toast("请先选择要上传的 PDF / JPEG / PNG 文件", true);
      return;
    }
    if (file.size > MAX_ATTACHMENT_BYTES) {
      toast(`文件超过 10 MiB 上限（当前 ${fmtBytes(file.size)}）`, true);
      return;
    }
    const uploadedBy = $("#att-uploader").value.trim();
    if (!uploadedBy) {
      toast("请填写上传人", true);
      return;
    }
    const form = new FormData();
    form.append("file", file);
    form.append("uploaded_by", uploadedBy);
    const btn = $("#btn-att-upload");
    btn.disabled = true;
    try {
      const resp = await fetch(`/api/events/${eventId}/attachments`, {
        method: "POST",
        body: form,
      });
      const payload = await resp.json().catch(() => null);
      if (!resp.ok) throw new Error(extractError(payload) || `上传失败（${resp.status}）`);
      toast(`附件已上传：${payload.filename}（实体 ${payload.digest.slice(0, 12)}…）`);
      $("#att-file").value = "";
      await renderAttachments(eventId);
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // 下载（blob 方式，保证浏览器按原文件名保存）
  async function downloadAttachment(id) {
    const resp = await fetch(`/api/attachments/${id}/download`);
    if (resp.status === 409) {
      const payload = await resp.json().catch(() => null);
      toast(extractError(payload) || "附件完整性校验失败（409）", true);
      await renderAttachments(attState.eventId);
      return;
    }
    if (!resp.ok) {
      const payload = await resp.json().catch(() => null);
      toast(extractError(payload) || `下载失败（${resp.status}）`, true);
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  $("#att-tbody").addEventListener("click", async (e) => {
    const dlBtn = e.target.closest("button[data-att-dl]");
    if (dlBtn) {
      await downloadAttachment(Number(dlBtn.dataset.attDl));
      return;
    }
    const refreshOneBtn = e.target.closest("button[data-att-refresh-one]");
    if (refreshOneBtn) {
      try {
        const a = await api(
          `/api/attachments/${refreshOneBtn.dataset.attRefreshOne}/refresh`,
          { method: "POST" }
        );
        toast(a.status === "corrupt" ? "复核完成：附件已损坏" : "复核完成：摘要一致 ✓");
      } catch (err) {
        toast(err.message, true);
      }
      await renderAttachments(attState.eventId);
      return;
    }
    const delBtn = e.target.closest("button[data-att-del]");
    if (delBtn) {
      const id = Number(delBtn.dataset.attDel);
      const target = (state.data.attachments || []).find((x) => x.id === id);
      if (
        !confirm(
          `确定删除附件「${target ? target.filename : id}」？\n` +
            "仅解除本条附件记录；若实体已无其他引用，将一并清理实体文件。"
        )
      )
        return;
      try {
        await api(`/api/attachments/${id}`, { method: "DELETE" });
        toast("附件记录已删除");
        await renderAttachments(attState.eventId);
      } catch (err) {
        toast(err.message, true);
      }
    }
  });

  $("#btn-att-refresh").addEventListener("click", async () => {
    try {
      const list = await api(`/api/events/${attState.eventId}/attachments/refresh`, {
        method: "POST",
      });
      const corrupt = list.filter((a) => a.status === "corrupt").length;
      toast(corrupt ? `完整性刷新完成：${corrupt} 个附件损坏` : "完整性刷新完成：全部摘要一致 ✓");
      await renderAttachments(attState.eventId);
    } catch (err) {
      toast(err.message, true);
    }
  });

  // ---------- 启动 ----------

  (async function init() {
    try {
      await loadSamples();
      if (state.samples.length) await selectSample(state.samples[0].id);
      else showEmpty();
    } catch (err) {
      toast("初始化失败：" + err.message, true);
    }
  })();
})();
