(() => {
  function getCsrf() {
    const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : (window.KHIDR.csrfToken || "");
  }
  const csrf = getCsrf();

  // --- live board via WebSocket -------------------------------------------
  const board = document.querySelector('[data-js="board"]');
  if (board) wireLiveBoard(board);

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function truncate(s, n) {
    s = String(s || "");
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  // "just now", "3m ago", "2h ago", "yesterday", "4d ago", "1w ago" …
  function humanizeAgo(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const s = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (s < 60) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    const days = Math.floor(h / 24);
    if (days === 1) return "yesterday";
    if (days < 7) return `${days}d ago`;
    if (days < 30) return `${Math.floor(days / 7)}w ago`;
    if (days < 365) return `${Math.floor(days / 30)}mo ago`;
    return `${Math.floor(days / 365)}y ago`;
  }

  function fullDate(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "";
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function agoHtml(iso) {
    if (!iso) return "";
    return `<time class="ago" datetime="${escapeHtml(iso)}" title="${fullDate(iso)}">${humanizeAgo(iso)}</time>`;
  }

  function refreshAgo() {
    document.querySelectorAll("time.ago[datetime]").forEach((el) => {
      const iso = el.getAttribute("datetime");
      el.textContent = humanizeAgo(iso);
      el.title = fullDate(iso);
    });
  }
  refreshAgo();
  setInterval(refreshAgo, 30000);

  function cardHtml(t) {
    const cpx = `<span class="cpx" title="complexity">c${t.complexity}</span>`;
    const agent = t.current_agent
      ? `<span class="agent" title="agent">${escapeHtml(t.current_agent)}</span>`
      : (t.assigned_agent
          ? `<span class="agent assigned" title="assigned">${escapeHtml(t.assigned_agent)}</span>`
          : "");
    const wait = t.waiting_for_user ? `<span class="wait" title="waiting for user">⏸</span>` : "";
    const approve = t.status === "review"
      ? `<button type="button" class="btn card-approve" data-js="approve-btn"
                data-task-id="${t.id}" title="Move to Done">✓ Approve</button>`
      : "";
    const todoBtn = t.status === "backlog"
      ? `<button type="button" class="btn card-todo" data-js="todo-btn"
                data-task-id="${t.id}" title="Move to Todo">→ Todo</button>`
      : "";
    return `
      <li class="card c-${t.complexity}" data-task-id="${t.id}">
        <button type="button" class="card-del" data-js="del-card"
                data-task-id="${t.id}" title="Delete">×</button>
        <a href="/tasks/${t.id}/">
          <span class="title">${escapeHtml(truncate(t.title, 80))}</span>
          <span class="meta">${cpx}${agent}${wait}${agoHtml(t.created_at)}</span>
        </a>
        ${approve}
        ${todoBtn}
      </li>`;
  }

  function reconcileBoard(tasks) {
    // columns keyed by status
    const cols = {};
    board.querySelectorAll('.col').forEach((c) => { cols[c.dataset.status] = c; });

    // group incoming by status, preserve order
    const byStatus = {};
    tasks.forEach((t) => {
      (byStatus[t.status] = byStatus[t.status] || []).push(t);
    });

    // 1. Remove cards whose id is no longer present anywhere.
    const incomingIds = new Set(tasks.map((t) => String(t.id)));
    board.querySelectorAll('li.card[data-task-id]').forEach((el) => {
      if (!incomingIds.has(el.dataset.taskId)) el.remove();
    });

    // 2. For each column, rebuild the ul in the order given by incoming[status].
    Object.keys(cols).forEach((status) => {
      const col = cols[status];
      const list = col.querySelector('[data-js="col-list"]');
      const rows = byStatus[status] || [];
      // Move/append card elements in sequence (minimal DOM churn).
      rows.forEach((t) => {
        let el = board.querySelector(`li.card[data-task-id="${t.id}"]`);
        if (!el) {
          const tmp = document.createElement("template");
          tmp.innerHTML = cardHtml(t).trim();
          el = tmp.content.firstChild;
        } else {
          // Update existing in place (title/complexity/agent/wait) — still
          // cheaper than full replace.
          const titleEl = el.querySelector(".title");
          if (titleEl) titleEl.textContent = truncate(t.title, 80);
          el.className = `card c-${t.complexity}`;
          const meta = el.querySelector(".meta");
          if (meta) {
            meta.innerHTML = ""
              + `<span class="cpx" title="complexity">c${t.complexity}</span>`
              + (t.current_agent
                  ? `<span class="agent" title="agent">${escapeHtml(t.current_agent)}</span>`
                  : (t.assigned_agent
                      ? `<span class="agent assigned" title="assigned">${escapeHtml(t.assigned_agent)}</span>`
                      : ""))
              + (t.waiting_for_user ? `<span class="wait" title="waiting for user">⏸</span>` : "")
              + agoHtml(t.created_at);
          }
          // sync approve button presence
          let approveBtn = el.querySelector('[data-js="approve-btn"]');
          if (t.status === "review") {
            if (!approveBtn) {
              approveBtn = document.createElement("button");
              approveBtn.type = "button";
              approveBtn.className = "btn card-approve";
              approveBtn.dataset.js = "approve-btn";
              approveBtn.dataset.taskId = t.id;
              approveBtn.title = "Move to Done";
              approveBtn.textContent = "✓ Approve";
              el.appendChild(approveBtn);
            }
          } else if (approveBtn) {
            approveBtn.remove();
          }
          // sync todo button presence
          let todoBtn = el.querySelector('[data-js="todo-btn"]');
          if (t.status === "backlog") {
            if (!todoBtn) {
              todoBtn = document.createElement("button");
              todoBtn.type = "button";
              todoBtn.className = "btn card-todo";
              todoBtn.dataset.js = "todo-btn";
              todoBtn.dataset.taskId = t.id;
              todoBtn.title = "Move to Todo";
              todoBtn.textContent = "→ Todo";
              el.appendChild(todoBtn);
            }
          } else if (todoBtn) {
            todoBtn.remove();
          }
        }
        list.appendChild(el);  // appendChild moves if already in DOM
      });
      // Update count badge
      const count = col.querySelector('[data-js="col-count"]');
      if (count) count.textContent = String(rows.length);
    });

    // 3. Re-bind handlers on any (possibly new) buttons.
    wireDeleteButtons();
    wireApproveButtons();
    wireTodoButtons();
  }

  function wireLiveBoard(board) {
    const pid = board.dataset.projectId;
    if (!pid) return;

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/projects/${pid}/`;
    let ws = null;
    let reconnectDelay = 1000;
    let timer = null;
    let closedByUs = false;

    function connect() {
      clearTimeout(timer);
      ws = new WebSocket(url);
      ws.onopen = () => { reconnectDelay = 1000; };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "board" && Array.isArray(msg.tasks)) {
          reconcileBoard(msg.tasks);
        }
      };
      ws.onclose = (ev) => {
        if (closedByUs) return;
        if (ev.code === 4401) return;  // auth lost
        timer = setTimeout(connect, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 15000);
      };
      ws.onerror = () => {};
    }
    window.addEventListener("beforeunload", () => { closedByUs = true; if (ws) ws.close(); });
    connect();
  }

  // Card × buttons are re-bound after every reconcile; this helper stays
  // idempotent by tracking an attribute flag.
  function wireDeleteButtons() {
    document.querySelectorAll('[data-js="del-card"]').forEach((btn) => {
      if (btn.dataset.wired === "1") return;
      btn.dataset.wired = "1";
      btn.addEventListener("click", handleCardDelete);
    });
  }

  async function handleCardDelete(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    const btn = ev.currentTarget;
    const id = btn.dataset.taskId;
    if (!confirm("Delete this task? This cannot be undone.")) return;
    btn.disabled = true;
    try {
      const r = await fetch(`/tasks/${id}/delete/`, {
        method: "POST",
        body: new URLSearchParams(),
        headers: { "X-CSRFToken": csrf },
        credentials: "same-origin",
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || r.statusText);
      // WebSocket will reconcile the card away; no manual DOM surgery here.
    } catch (e) {
      alert("Could not delete: " + String(e).replace(/^Error:\s*/, ""));
      btn.disabled = false;
    }
  }

  function wireApproveButtons() {
    document.querySelectorAll('[data-js="approve-btn"]').forEach((btn) => {
      if (btn.dataset.wired === "1") return;
      btn.dataset.wired = "1";
      btn.addEventListener("click", handleCardApprove);
    });
  }

  async function handleCardApprove(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    const btn = ev.currentTarget;
    const id = btn.dataset.taskId;
    btn.disabled = true;
    btn.classList.add("loading");
    try {
      const r = await fetch(`/tasks/${id}/approve/`, {
        method: "POST",
        body: new URLSearchParams(),
        headers: { "X-CSRFToken": csrf },
        credentials: "same-origin",
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || r.statusText);
      // WebSocket will reconcile the card to Done column.
    } catch (e) {
      alert("Could not approve: " + String(e).replace(/^Error:\s*/, ""));
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  }

  function wireTodoButtons() {
    document.querySelectorAll('[data-js="todo-btn"]').forEach((btn) => {
      if (btn.dataset.wired === "1") return;
      btn.dataset.wired = "1";
      btn.addEventListener("click", handleCardMoveToTodo);
    });
  }

  async function handleCardMoveToTodo(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    const btn = ev.currentTarget;
    const id = btn.dataset.taskId;
    if (!confirm("Move this card to Todo?")) return;
    btn.disabled = true;
    btn.classList.add("loading");
    try {
      const r = await fetch(`/tasks/${id}/move_to_todo/`, {
        method: "POST",
        body: new URLSearchParams(),
        headers: { "X-CSRFToken": csrf },
        credentials: "same-origin",
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || r.statusText);
      // WebSocket will reconcile the card to Todo column.
    } catch (e) {
      alert("Could not move to Todo: " + String(e).replace(/^Error:\s*/, ""));
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  }

  const dialog = document.querySelector('[data-js="new-task-dialog"]');
  const title = dialog.querySelector('[data-js="t-title"]');
  const desc = dialog.querySelector('[data-js="t-desc"]');
  const cpx = dialog.querySelector('[data-js="t-cpx"]');
  const status = dialog.querySelector('[data-js="t-status"]');
  const agentSel = dialog.querySelector('[data-js="t-agent"]');
  const cancel = dialog.querySelector('[data-js="new-task-cancel"]');
  const submit = dialog.querySelector('[data-js="new-task-submit"]');

  let currentProjectId = null;

  document.querySelectorAll('[data-js="new-task-btn"]').forEach((btn) => {
    btn.addEventListener("click", () => {
      currentProjectId = btn.dataset.projectId;
      // populate agent list from the hidden <template> rendered per project
      agentSel.innerHTML = "";
      const tpl = document.querySelector(`[data-js="agents-for-${currentProjectId}"]`);
      if (tpl) agentSel.innerHTML = tpl.innerHTML;
      title.value = "";
      desc.value = "";
      cpx.value = "3";
      status.value = "todo";
      dialog.showModal();
      setTimeout(() => title.focus(), 50);
    });
  });

  cancel.addEventListener("click", () => dialog.close());

  // Wire existing buttons once on load; reconcile re-wires newly rendered ones.
  wireDeleteButtons();
  wireApproveButtons();
  wireTodoButtons();

  submit.addEventListener("click", async () => {
    if (!title.value.trim()) { title.focus(); return; }
    submit.disabled = true;
    try {
      const body = new URLSearchParams();
      body.append("project_id", currentProjectId);
      body.append("title", title.value.trim());
      body.append("description", desc.value.trim());
      body.append("complexity", cpx.value);
      body.append("status", status.value);
      body.append("assigned_agent", agentSel.value);
      const r = await fetch("/tasks/create/", {
        method: "POST", body,
        headers: { "X-CSRFToken": csrf },
        credentials: "same-origin",
      });
      if (!r.ok) throw new Error(await r.text());
      dialog.close();
      location.reload();
    } catch (e) {
      alert("Could not create: " + e);
    } finally {
      submit.disabled = false;
    }
  });
})();
