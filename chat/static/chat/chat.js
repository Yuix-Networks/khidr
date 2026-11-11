(() => {
  const wrap = document.querySelector(".chat-wrap");
  if (!wrap) return;

  const taskId = Number(wrap.dataset.taskId);

  function getCsrf() {
    // Always prefer the live cookie — Django rotates the token on login,
    // and the server-rendered {{ csrf_token }} can be stale across tabs.
    const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : (window.KHIDR.csrfToken || "");
  }

  const body = wrap.querySelector('[data-js="messages"]');
  const input = wrap.querySelector('[data-js="input"]');
  const sendBtn = wrap.querySelector('[data-js="send-btn"]');
  const hint = wrap.querySelector('[data-js="hint"]');
  const statusPill = wrap.querySelector('[data-js="status-pill"]');
  const agentPill = wrap.querySelector('[data-js="agent-pill"]');
  const branchPill = wrap.querySelector('[data-js="branch-pill"]');
  const contextPill = wrap.querySelector('[data-js="context-pill"]');
  const agentStatusPill = wrap.querySelector('[data-js="agent-status-pill"]');
  // Always-visible Approve button in the header row-end.
  const approveBtns = wrap.querySelectorAll('[data-js="approve-btn"]');

  const editBtn = wrap.querySelector('[data-js="edit-btn"]');
  const deleteBtn = wrap.querySelector('[data-js="delete-btn"]');
  const closeAndCreateBtn = wrap.querySelector('[data-js="close-and-create-btn"]');
  const editDialog = wrap.parentElement.querySelector('[data-js="edit-dialog"]');
  const closeAndCreateDialog = wrap.parentElement.querySelector('[data-js="close-and-create-dialog"]');
  const cacTitle = closeAndCreateDialog && closeAndCreateDialog.querySelector('[data-js="cac-title"]');
  const editTitle = editDialog && editDialog.querySelector('[data-js="e-title"]');
  const editDesc = editDialog && editDialog.querySelector('[data-js="e-desc"]');
  const editCpx = editDialog && editDialog.querySelector('[data-js="e-cpx"]');
  const editAgent = editDialog && editDialog.querySelector('[data-js="e-agent"]');
  const editSubmit = editDialog && editDialog.querySelector('[data-js="edit-submit"]');
  const editCancel = editDialog && editDialog.querySelector('[data-js="edit-cancel"]');
  const editErr = editDialog && editDialog.querySelector('[data-js="edit-err"]');
  const cacSubmit = closeAndCreateDialog && closeAndCreateDialog.querySelector('[data-js="cac-submit"]');
  const cacCancel = closeAndCreateDialog && closeAndCreateDialog.querySelector('[data-js="cac-cancel"]');
  const cacErr = closeAndCreateDialog && closeAndCreateDialog.querySelector('[data-js="cac-err"]');

  // -------- initial render from server-provided JSON ----------
  const initial = JSON.parse(document.getElementById("initial-messages").textContent || "[]");
  initial.forEach(appendMessage);
  scrollToBottom(true);

  // -------- WebSocket with auto-reconnect ----------
  const wsProto = location.protocol === "https:" ? "wss" : "ws";
  const wsUrl = `${wsProto}://${location.host}/ws/tasks/${taskId}/`;
  let ws = null;
  let reconnectDelay = 1000;
  let reconnectTimer = null;
  let manuallyClosed = false;

  function connect() {
    clearTimeout(reconnectTimer);
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      reconnectDelay = 1000;
      setHint(wrap.dataset.waiting === "1" ? "⏸ waiting_for_user" : "", "ok");
    };

    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === "chat") {
        if (document.querySelector(`[data-msg-id="${msg.id}"]`)) return;
        appendMessage({ id: msg.id, role: msg.role, agent: msg.agent, content: msg.content, created_at: msg.created_at });
        scrollToBottom();
      } else if (msg.type === "task") {
        applyTaskUpdate(msg);
      }
    };

    ws.onclose = (ev) => {
      if (manuallyClosed) return;
      if (ev.code === 4401) {
        setHint("🔒 oturum sona erdi — yeniden giriş yap", "err");
        return;
      }
      setHint(`🔌 bağlantı yok, yeniden deneniyor...`, "warn");
      reconnectTimer = setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    };

    ws.onerror = () => {
      // onclose will fire right after.
    };
  }

  window.addEventListener("beforeunload", () => { manuallyClosed = true; if (ws) ws.close(); });

  function setHint(text, tone) {
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle("err", tone === "err");
    hint.classList.toggle("warn", tone === "warn");
  }

  connect();

  // -------- message rendering ----------
  function appendMessage(m) {
    const el = document.createElement("div");
    el.dataset.msgId = m.id;

    const isToolEntry = m.role === "system" && typeof m.content === "string" && m.content.startsWith("```");
    const isNoteEntry = m.role === "system" && typeof m.content === "string" && m.content.startsWith("📝");

    if (isToolEntry) {
      el.className = "msg tool";
      const det = document.createElement("details");
      det.className = "tool-details";
      const sum = document.createElement("summary");
      const parsed = tryParseToolBlock(m.content);
      const time = `<span class="time">${fmtTime(m.created_at)}</span>`;
      sum.innerHTML = parsed
        ? `<span class="name">${escapeHtml(parsed.tool)}</span> <span class="muted">${escapeHtml(parsed.brief)}</span>${time}`
        : `<span class="muted">system</span>${time}`;
      det.appendChild(sum);
      det.appendChild(prettifyToolContent(m.content));
      el.appendChild(det);
    } else if (isNoteEntry) {
      el.className = "msg note";
      const det = document.createElement("details");
      det.className = "note-details";
      det.setAttribute("open", ""); // notes open by default
      const sum = document.createElement("summary");
      sum.textContent = "📝 note";
      const body = document.createElement("div");
      body.className = "note-body";
      body.innerHTML = renderMarkdown(extractNoteText(m.content));
      det.appendChild(sum);
      det.appendChild(body);
      el.appendChild(det);
    } else {
      el.className = `msg ${m.role}`;
      const hdr = document.createElement("div");
      hdr.className = "hdr";
      hdr.innerHTML = `${m.agent ? escapeHtml(m.agent) : escapeHtml(m.role)} <span class="time">${fmtTime(m.created_at)}</span>`;
      const bd = document.createElement("div");
      bd.textContent = m.content || "";
      el.appendChild(hdr);
      el.appendChild(bd);
    }
    body.appendChild(el);
  }

  function prettifyToolContent(content) {
    // Render structure:
    //   <pre>  JSON of tool/args/detail (result field elided)  </pre>
    //   <details> <summary>result</summary>
    //     <pre>  raw string of result (newlines + escapes resolved)  </pre>
    //   </details>
    //
    // The result block is always collapsible when present — regardless of
    // length — so the agent's tool-call cards stay compact while the
    // raw output is one click away.
    const container = document.createElement("div");
    container.className = "tool-content";

    const match = content.match(/```\n([\s\S]*?)\n```/);
    if (!match) {
      const pre = document.createElement("pre");
      pre.textContent = content.replace(/^```\n?|```$/g, "");
      container.appendChild(pre);
      return container;
    }

    let obj;
    try {
      obj = JSON.parse(match[1]);
    } catch {
      const pre = document.createElement("pre");
      pre.textContent = match[1];
      container.appendChild(pre);
      return container;
    }

    // For ask_user the useful payload is the agent's question (args.message).
    // The result (a short "waiting for user" ack) isn't informative. Swap
    // the secondary block out for the question and default-open it so the
    // reader actually sees what they're being asked.
    const isAskUser = obj.tool === "ask_user";

    let secondaryLabel = "result";
    let secondaryValue;
    let secondaryOpen = false;
    if (isAskUser && obj.args && typeof obj.args.message === "string") {
      secondaryLabel = "message";
      secondaryValue = obj.args.message;
      secondaryOpen = true;
    } else if (obj.result !== undefined) {
      secondaryValue = obj.result;
    } else if (obj.detail && obj.detail.result !== undefined) {
      // Legacy path — older runs stashed the tool result inside detail.
      secondaryValue = obj.detail.result;
    }

    // Body = the tool call minus whatever we rendered separately below.
    const body = { ...obj };
    if ("result" in body) delete body.result;
    if (body.detail && "result" in body.detail) {
      body.detail = { ...body.detail };
      delete body.detail.result;
    }
    if (isAskUser && body.args && "message" in body.args) {
      body.args = { ...body.args };
      delete body.args.message;
    }
    const headPre = document.createElement("pre");
    headPre.textContent = JSON.stringify(body, null, 2);
    container.appendChild(headPre);

    if (secondaryValue !== undefined) {
      const valueStr = typeof secondaryValue === "string"
        ? secondaryValue
        : JSON.stringify(secondaryValue, null, 2);

      const det = document.createElement("details");
      det.className = "result-collapse";
      if (secondaryOpen) det.setAttribute("open", "");
      const sum = document.createElement("summary");
      // Hint at length so the reader knows whether it's worth opening.
      const lines = valueStr.split("\n").length;
      const bytes = valueStr.length;
      sum.textContent = valueStr
        ? `${secondaryLabel} (${lines} line${lines === 1 ? "" : "s"}, ${bytes} bytes)`
        : `${secondaryLabel} (empty)`;
      const innerPre = document.createElement("pre");
      innerPre.textContent = valueStr;
      det.appendChild(sum);
      det.appendChild(innerPre);
      container.appendChild(det);
    }

    return container;
  }

  function extractNoteText(content) {
    // Strip the leading "📝 **note**\n\n" prefix
    return content.replace(/^📝 \*\*note\*\*\s*\n*/i, "");
  }

 function renderMarkdown(text) {
  // Full markdown → HTML renderer for note bodies.
  // XSS-safe: code blocks and inline code are protected with unique
  // placeholders before any HTML escaping / markdown conversion runs.
  if (!text) return "";

  const codeBlocks = [];
  const rawInlineCodes = [];
  let sanitized = text;

  // --- 1. Protect fenced code blocks (```...```) ---
  sanitized = sanitized.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
   const idx = codeBlocks.length;
   codeBlocks.push(
    `<pre class="code-block"><code class="language-${lang || 'text'}">${escapeHtml(code.trimEnd())}</code></pre>`
   );
   return `\x00CODEBLOCK${idx}\x00`;
  });

  // --- 2. Protect inline code (`...`) ---
  sanitized = sanitized.replace(/`([^`]+)`/g, (_, code) => {
   rawInlineCodes.push(code);
   return `\x00INLINECODE${rawInlineCodes.length - 1}\x00`;
  });

  // --- 3. Escape raw HTML in remaining text (security) ---
  sanitized = escapeHtml(sanitized);

  // --- 4. Markdown → HTML conversion ---
  sanitized = sanitized
   // Headers
   .replace(/^### (.+)$/gm, "<h3>$1</h3>")
   .replace(/^## (.+)$/gm, "<h2>$1</h2>")
   .replace(/^# (.+)$/gm, "<h1>$1</h1>")
   // Bold
   .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
   // Italic
   .replace(/\*(.+?)\*/g, "<em>$1</em>")
   // Blockquote
   .replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>")
   // Unordered list items
   .replace(/^[-*] (.+)$/gm, "<li>$1</li>")
   // Ordered list items
   .replace(/^\d+\. (.+)$/gm, "<li>$1</li>")
   // Wrap consecutive <li> in <ul>
   .replace(/(<li>.*?<\/li>(\n|$))+/g, (match) => `<ul>${match.replace(/\n/g, "")}</ul>`)
   // Horizontal rule
   .replace(/^---$/gm, "<hr>")
   // Paragraphs: blank-line separated blocks become <p>
   .replace(/\n\n+/g, "</p><p>")
   // Single line breaks inside inline content → <br>
   .replace(/\n(?![<]\/<|li>|<ul>|<ol|<h|<blockquote|<pre|<hr|<p)/g, "<br>")
  ;

  // --- 5. Restore inline code ---
  rawInlineCodes.forEach((code, i) => {
   sanitized = sanitized.replace(`\x00INLINECODE${i}\x00`,
    `<code class="inline-code">${escapeHtml(code)}</code>`);
  });

  // --- 6. Restore fenced code blocks ---
  codeBlocks.forEach((html, i) => {
   sanitized = sanitized.replace(`\x00CODEBLOCK${i}\x00`, html);
  });

  // Wrap bare text in <p> if it doesn't start with a block-level tag
  const trimmed = sanitized.trim();
  if (trimmed && !/^<(h[1-6]|ul|ol|pre|blockquote|hr)/.test(trimmed)) {
   return `<p>${trimmed}</p>`;
  }
  return trimmed;
 }

  function tryParseToolBlock(content) {
    // content looks like: ```\n{...}\n```
    const match = content.match(/```\n([\s\S]*?)\n```/);
    if (!match) return null;
    try {
      const obj = JSON.parse(match[1]);
      const argBrief = obj.args ? JSON.stringify(obj.args).slice(0, 80) : "";
      return { tool: obj.tool || "tool", brief: argBrief };
    } catch { return null; }
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toTimeString().slice(0, 8);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>\"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function scrollToBottom(force) {
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 200;
    if (force || nearBottom) body.scrollTop = body.scrollHeight;
  }

  // -------- task state sync ----------
  let lastTps = 0;
  function applyTaskUpdate(t) {
    statusPill.textContent = statusLabel(t.status);
    statusPill.dataset.status = t.status;

    if (t.current_agent) agentPill.textContent = "🤖 " + t.current_agent;
    else if (t.assigned_agent) agentPill.textContent = "assigned: " + t.assigned_agent;
    else agentPill.textContent = "—";

    if (branchPill) branchPill.textContent = t.branch_name ? "branch: " + t.branch_name : "";

    // Agent status indicator
    if (agentStatusPill) {
      const status = t.agent_status || "idle";
      const detail = t.agent_status_detail || "";
      const labels = {
        thinking: "🤔 düşünüyor",
        tool_call: detail ? `🔧 ${detail}` : "🔧 tool çağrıyor",
        writing: "✍️ yazıyor",
        idle: "⏸ bekliyor",
        done: "✅ tamamlandı",
      };
      agentStatusPill.textContent = labels[status] || status;
      agentStatusPill.className = "pill agent-status status-" + status;
    }

    if (contextPill) {
      const used = t.context_used || 0;
      const limit = t.context_limit || 0;
      const tps = t.tokens_per_second || 0;
      lastTps = tps;
      if (limit > 0) {
        const pct = Math.round((used / limit) * 100);
        const left = limit - used;
        contextPill.textContent = `ctx: ${formatTokenCount(used)}/${formatTokenCount(limit)} · ${tps.toFixed(1)} tok/s`;
        contextPill.dataset.used = used;
        contextPill.dataset.limit = limit;
        contextPill.title = `~${formatTokenCount(left)} tokens left`;
      } else {
        contextPill.textContent = tps > 0 ? `${tps.toFixed(1)} tok/s` : "ctx: —";
      }
    }

    hint.textContent = t.waiting_for_user ? "⏸ waiting_for_user" : "";
    input.placeholder = t.waiting_for_user ? "Agent senden cevap bekliyor..." : "Mesaj yaz (Cmd/Ctrl+Enter)";

    wrap.dataset.status = t.status;
    wrap.dataset.waiting = t.waiting_for_user ? "1" : "0";
  }

  function formatTokenCount(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
    if (n >= 1000) return (n / 1000).toFixed(1) + "K";
    return String(n);
  }

  function statusLabel(s) {
    return ({ backlog: "Backlog", todo: "Todo", in_progress: "In Progress", review: "Review", done: "Done" }[s] || s);
  }

  // -------- POST actions ----------
  async function post(url, data) {
    // URLSearchParams → application/x-www-form-urlencoded. Avoids a
    // Daphne/ASGI edge case where multipart bodies (especially empty
    // ones sent by Firefox's FormData) are rejected with 400 before
    // any Django middleware or view runs.
    const body = new URLSearchParams();
    Object.entries(data || {}).forEach(([k, v]) => body.append(k, v));
    const r = await fetch(url, {
      method: "POST",
      body,
      headers: { "X-CSRFToken": getCsrf() },
      credentials: "same-origin",
    });
    return r.ok ? r.json() : Promise.reject(await r.text().catch(() => r.statusText));
  }

  async function sendMessage() {
    const txt = input.value.trim();
    if (!txt) return;
    input.value = "";
    sendBtn.disabled = true;
    try {
      await post(`/tasks/${taskId}/send/`, { text: txt });
    } catch (e) {
      alert("Gönderilemedi: " + e);
      input.value = txt;
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  sendBtn.addEventListener("click", sendMessage);
  input.addEventListener("keydown", (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
      ev.preventDefault();
      sendMessage();
    }
  });

  approveBtns.forEach((btn) => {
    btn.addEventListener("click", async () => {
      // Lock every Approve button at once so a double-click on the
      // header button doesn't fire while the review-actions button is
      // still resolving (or vice versa). Always release in finally —
      // earlier we only released on error, which left the header
      // button stuck spinning forever on the happy path (the review
      // one was hidden by the status change, so the bug was invisible
      // until we added the always-on row-end button).
      const lock = (on) =>
        approveBtns.forEach((b) => {
          b.disabled = on;
          b.classList.toggle("loading", on);
        });
      lock(true);
      try {
        await post(`/tasks/${taskId}/approve/`, {});
        // Approved: keep every Approve button disabled and relabel it.
        approveBtns.forEach((b) => {
          b.classList.remove("loading");
          b.textContent = "✓ Approved";
        });
      } catch (e) {
        alert("Onaylanamadı: " + e);
        lock(false);
      }
    });
  });

  if (editBtn) {
    editBtn.addEventListener("click", () => {
      if (editErr) editErr.textContent = "";
      editDialog.showModal();
      setTimeout(() => editTitle.focus(), 50);
    });
    editCancel.addEventListener("click", () => editDialog.close());
    editSubmit.addEventListener("click", async () => {
      if (!editTitle.value.trim()) { editTitle.focus(); return; }
      editSubmit.disabled = true;
      editErr.textContent = "";
      try {
        await post(`/tasks/${taskId}/edit/`, {
          title: editTitle.value.trim(),
          description: editDesc.value,
          complexity: editCpx.value,
          assigned_agent: editAgent.value,
        });
        editDialog.close();
        location.reload();
      } catch (e) {
        editErr.textContent = String(e).replace(/^Error:\s*/, "");
      } finally {
        editSubmit.disabled = false;
      }
    });
  }

  if (deleteBtn) {
    deleteBtn.addEventListener("click", async () => {
      if (!confirm("Bu task'ı sil? Geri alınamaz (chat geçmişi dahil).")) return;
      deleteBtn.disabled = true;
      try {
        const r = await post(`/tasks/${taskId}/delete/`, {});
        location.href = r.project_id ? `/projects/${r.project_id}/` : "/";
      } catch (e) {
        alert("Silinemedi: " + e);
        deleteBtn.disabled = false;
      }
    });
  }

  if (closeAndCreateBtn && closeAndCreateDialog) {
    closeAndCreateBtn.addEventListener("click", () => {
      if (cacErr) cacErr.textContent = "";
      cacTitle.value = "";
      closeAndCreateDialog.showModal();
      setTimeout(() => cacTitle.focus(), 50);
    });
    if (cacCancel) {
      cacCancel.addEventListener("click", () => closeAndCreateDialog.close());
    }
    if (cacSubmit) {
      cacSubmit.addEventListener("click", async () => {
        if (!cacTitle.value.trim()) { cacTitle.focus(); return; }
        cacSubmit.disabled = true;
        if (cacErr) cacErr.textContent = "";
        try {
          const r = await post(`/tasks/${taskId}/close_and_create/`, {
            title: cacTitle.value.trim(),
          });
          if (r.url) {
            location.href = r.url;
          } else {
            location.reload();
          }
        } catch (e) {
          if (cacErr) cacErr.textContent = String(e).replace(/^Error:\s*/, "");
        } finally {
          cacSubmit.disabled = false;
        }
      });
    }
  }
})();
