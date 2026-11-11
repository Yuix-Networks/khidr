(() => {
  function getCsrf() {
    const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : (window.KHIDR.csrfToken || "");
  }
  const csrf = getCsrf();

  const dialog = document.querySelector('[data-js="new-project-dialog"]');
  const openBtn = document.querySelector('[data-js="new-project-btn"]');
  const cancelBtn = document.querySelector('[data-js="p-cancel"]');
  const submit = document.querySelector('[data-js="p-submit"]');
  const name = document.querySelector('[data-js="p-name"]');
  const desc = document.querySelector('[data-js="p-desc"]');
  const err = document.querySelector('[data-js="p-err"]');

  // --- quick filter: "/" focuses, Enter opens the first visible match ----
  const filter = document.querySelector('[data-js="p-filter"]');
  const cards = Array.from(document.querySelectorAll(".project-card"));
  const noMatch = document.querySelector('[data-js="p-nomatch"]');
  function applyFilter() {
    const q = filter.value.trim().toLowerCase();
    let shown = 0;
    cards.forEach((c) => {
      const hit = !q || (c.dataset.name || "").includes(q);
      c.hidden = !hit;
      if (hit) shown++;
    });
    if (noMatch) noMatch.hidden = shown > 0 || cards.length === 0;
  }
  if (filter) {
    filter.addEventListener("input", applyFilter);
    filter.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        const first = cards.find((c) => !c.hidden);
        if (first) location.href = first.href;
      } else if (ev.key === "Escape") {
        filter.value = "";
        applyFilter();
        filter.blur();
      }
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && !dialog.open) {
        ev.preventDefault();
        filter.focus();
      }
    });
  }

  openBtn.addEventListener("click", () => {
    err.textContent = "";
    dialog.showModal();
    // Defer focus until after the show transition so the caret lands in
    // the name field, not on the button that opened the modal.
    setTimeout(() => name.focus(), 50);
  });

  cancelBtn.addEventListener("click", () => dialog.close());

  submit.addEventListener("click", async () => {
    err.textContent = "";
    const n = name.value.trim();
    if (!n) { name.focus(); return; }
    submit.disabled = true;
    try {
      const body = new URLSearchParams();
      body.append("name", n);
      body.append("description", desc.value.trim());
      const r = await fetch("/projects/create/", {
        method: "POST", body,
        headers: { "X-CSRFToken": csrf },
        credentials: "same-origin",
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || r.statusText);
      location.href = data.url || "/";
    } catch (e) {
      err.textContent = String(e).replace(/^Error:\s*/, "");
    } finally {
      submit.disabled = false;
    }
  });
})();
