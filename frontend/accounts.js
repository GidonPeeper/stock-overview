/* "Add an account" + "Connected accounts" on the Settings page.
 *
 * Everything is rendered from /api/institutions (backend/broker_catalog.json
 * plus what the app can do with each method), so a bigger catalog needs no
 * changes here: any number of institutions, any method type.
 * Uses globals from settings.html: savedMsg, loadCash, renderStatus, CUR.
 */
(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const https = u => (/^https:\/\//i.test(u || "") ? u : "");
  const norm = s => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

  const KIND = {broker: "Broker", bank: "Bank", both: "Bank & broker", crypto: "Crypto", robo: "Robo-adviser"};
  const GROUPS = [["broker", "Brokers"], ["both", "Banks & brokers"], ["robo", "Robo-advisers"],
                  ["crypto", "Crypto"], ["bank", "Banks"]];
  const REC_TAG = {api: "API", aggregator: "Bank link", upload: "File", manual: "Manual"};
  const COVERS = {positions: "positions", holdings: "positions", cash: "cash", transactions: "trades",
                  dividends: "dividends", interest: "interest", balance: "balance"};
  const FIELD_LABEL = {symbol: "Symbol / ticker", isin: "ISIN", name: "Name", quantity: "Quantity",
                       cost: "Cost", value: "Market value", price: "Price", currency: "Currency"};

  let INST = [], AGG = {}, country = "", active = -1, shown = [], picked = null, tab = null;
  let csvFile = null, csvMapping = null, csvTimer = null;

  const curDefault = () => { try { return CUR || "GBP"; } catch (e) { return "GBP"; } };
  const ok = (el, text) => { el.textContent = text; el.className = "msg ok"; };
  const bad = (el, text) => { el.textContent = "✗ " + text; el.className = "msg err"; };
  const info = (el, text) => { el.textContent = text; el.className = "msg"; };
  const detail = async r => { const e = await r.json().catch(() => ({})); return typeof e.detail === "string" ? e.detail : "Something went wrong."; };
  const syncNote = () => (window.SYNC_ON ? " Saved permanently (cloud-synced)." : "");
  const afterChange = () => {
    try { loadCash(); } catch (e) {}
    fetch("/api/datastatus").then(r => r.json()).then(s => { try { renderStatus(s); } catch (e) {} }).catch(() => {});
    loadConnections();
  };

  /* ------------------------------------------------------------ catalog */
  async function loadCatalog() {
    try {
      const cat = await (await fetch("/api/institutions")).json();
      INST = cat.institutions || [];
      AGG = Object.fromEntries((cat.aggregators || []).map(a => [norm(a.id), a]));
    } catch (e) {
      $("acct-panel").innerHTML = '<div class="msg err">Could not load the list of brokers and banks.</div>';
      return;
    }
    renderChips();
  }

  function renderChips() {
    const all = [...new Set(INST.flatMap(i => i.countries || []))].sort();
    $("acct-chips").innerHTML = ["", ...all].map(c =>
      `<button type="button" class="chip ${c === country ? "on" : ""}" data-c="${esc(c)}">${c ? esc(c) : "All countries"}</button>`).join("");
  }
  $("acct-chips").addEventListener("click", e => {
    const b = e.target.closest(".chip"); if (!b) return;
    country = b.dataset.c; renderChips(); openList();
  });

  /* --------------------------------------------------------- combobox */
  function matches(i, q) {
    if (country && (i.countries || []).length && !(i.countries || []).includes(country)) return false;
    if (!q) return true;
    const hay = norm(i.name + " " + i.id + " " + (KIND[i.kind] || ""));
    return q.split(/\s+/).every(w => hay.includes(norm(w)));
  }
  function openList() {
    const q = $("acct-q").value.trim();
    const list = $("acct-list");
    shown = [];
    let html = "";
    const known = new Set(GROUPS.map(g => g[0]));
    const groups = [...GROUPS, ["", "Other"]];
    for (const [kind, title] of groups) {
      const items = INST.filter(i => (kind ? i.kind === kind : !known.has(i.kind)) && matches(i, q));
      if (!items.length) continue;
      html += `<div class="grp">${esc(title)}</div>`;
      for (const i of items) {
        const n = shown.push(i) - 1;
        const meta = [(i.countries || []).join(" · "), KIND[i.kind] || ""].filter(Boolean).join(" · ");
        const tag = REC_TAG[i.recommended] || "";
        html += `<div class="opt" role="option" id="opt-${n}" data-n="${n}"><span class="opt-name">${esc(i.name)}
          <span class="opt-meta">${esc(meta)}</span></span>${tag ? `<span class="tag">${esc(tag)}</span>` : ""}</div>`;
      }
    }
    list.innerHTML = html || '<div class="opt" style="cursor:default;color:var(--faint)">No match. Try “Another broker” or “Another bank”.</div>';
    list.hidden = false;
    $("acct-q").setAttribute("aria-expanded", "true");
    active = -1;
  }
  function closeList() { $("acct-list").hidden = true; $("acct-q").setAttribute("aria-expanded", "false"); }
  function highlight(n) {
    document.querySelectorAll("#acct-list .opt.act").forEach(o => o.classList.remove("act"));
    const el = $("opt-" + n); if (!el) return;
    el.classList.add("act"); el.scrollIntoView({block: "nearest"});
    $("acct-q").setAttribute("aria-activedescendant", el.id);
  }
  $("acct-q").addEventListener("focus", openList);
  $("acct-q").addEventListener("input", openList);
  $("acct-q").addEventListener("keydown", e => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); if ($("acct-list").hidden) openList();
      active = Math.max(0, Math.min(shown.length - 1, active + (e.key === "ArrowDown" ? 1 : -1)));
      highlight(active);
    } else if (e.key === "Enter") {
      e.preventDefault(); const i = shown[active >= 0 ? active : 0]; if (i) pick(i);
    } else if (e.key === "Escape") closeList();
  });
  $("acct-list").addEventListener("mousedown", e => e.preventDefault()); // keep focus for clicks
  $("acct-list").addEventListener("click", e => {
    const o = e.target.closest(".opt[data-n]"); if (o) pick(shown[+o.dataset.n]);
  });
  document.addEventListener("click", e => { if (!e.target.closest(".combo")) closeList(); });

  /* ------------------------------------------------------------ tabs */
  // Aggregators and unofficial routes are grouped into one tab each.
  function tabsFor(inst) {
    const tabs = [];
    for (const m of inst.methods) {
      const group = m.type === "aggregator" ? "agg" : m.type === "unofficial" ? "unofficial" : null;
      let t = group && tabs.find(x => x.key === group);
      if (!t) {
        t = {key: group || m.key, methods: [], rec: false, label: tabLabel(m, group)};
        tabs.push(t);
      }
      t.methods.push(m);
      if (m.recommended) t.rec = true;
    }
    for (const t of tabs) t.usable = t.methods.some(m => ["live", "import", "csv", "manual"].includes(m.support));
    return tabs;
  }
  function tabLabel(m, group) {
    if (group === "agg") return "Bank link";
    if (group === "unofficial") return "Unofficial";
    if (m.type === "manual") return "Enter by hand";
    if (m.type === "upload") {
      if (m.synthetic) return "Positions file";
      const r = String(m.report_name || "file").replace(/\s*\(.*?\)\s*/g, " ").trim();
      return "File: " + (r.length > 26 ? r.slice(0, 25) + "…" : r);
    }
    if (m.type === "api") return {flex_token_query: "Flex report (API)", token: "API token", oauth: "API (app login)",
                                  other: "API"}[m.auth] || "API key";
    return m.type;
  }

  function pick(inst) {
    picked = inst; closeList();
    $("acct-q").value = inst.name;
    const tabs = tabsFor(inst);
    const def = tabs.find(t => t.methods.some(m => m.key === inst.default)) || tabs[0];
    tab = def ? def.key : null;
    renderPanel();
  }

  function renderPanel() {
    const inst = picked; if (!inst) return;
    const tabs = tabsFor(inst);
    const meta = [KIND[inst.kind] || "", (inst.countries || []).join(", ")].filter(Boolean).join(" · ");
    $("acct-panel").innerHTML = `<div class="acct-head"><b>${esc(inst.name)}</b>
        <div class="cmeta">${esc(meta)}</div></div>
      <div class="chips" role="tablist">${tabs.map(t => `<button type="button" role="tab" class="chip ${t.key === tab ? "on" : ""} ${t.usable ? "" : "dim"}"
        aria-selected="${t.key === tab}" data-tab="${esc(t.key)}">${esc(t.label)}${t.rec ? " · Recommended" : ""}</button>`).join("")}</div>
      <div id="acct-method"></div>`;
    $("acct-panel").querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => { tab = b.dataset.tab; renderPanel(); }));
    const t = tabs.find(x => x.key === tab) || tabs[0];
    if (!t) return;
    const box = $("acct-method");
    if (t.key === "agg") box.innerHTML = aggregatorHtml(inst, t.methods);
    else if (t.key === "unofficial") box.innerHTML = unofficialHtml(inst, t.methods);
    else renderMethod(box, inst, t.methods[0]);
  }

  /* -------------------------------------------------------- pieces */
  function facts(m) {
    const covers = [...new Set((m.covers || []).map(c => COVERS[c] || c))];
    const parts = [];
    if (covers.length) parts.push(`<span>Tracks: <b>${esc(covers.join(", "))}</b></span>`);
    if (m.updates) parts.push(`<span>Updates: <b>${esc(m.updates.toLowerCase())}</b></span>`);
    return parts.length ? `<div class="facts">${parts.join("")}</div>` : "";
  }
  function stepsHtml(m) {
    const steps = (m.steps || []).filter(Boolean);
    return steps.length ? `<ol class="steps">${steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>` : "";
  }
  function unverified(m, inst) {
    if (m.verified !== false) return "";
    return `<div class="note warn">Unverified: these steps come from other users' guides, not from ${esc(inst.name)}
      itself. Check ${esc(inst.name)}'s help pages if something looks different.</div>`;
  }
  function docLink(m, text) {
    const u = https(m.docs_url);
    return u ? `<div style="margin:6px 0"><a class="doc" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(text || "Official guide")} ↗</a></div>` : "";
  }
  const notes = m => (m.notes ? `<div class="note">${esc(m.notes)}</div>` : "");
  const others = inst => {
    const names = tabsFor(inst).filter(t => t.usable && t.key !== tab).map(t => `“${t.label}”`);
    return names.length ? ` Try ${names.join(" or ")} instead.` : "";
  };
  function isaBox(inst) {
    return (inst.wrappers || []).includes("ISA")
      ? `<label class="chk"><input type="checkbox" id="cf-isa" /> This is a Stocks &amp; Shares ISA (tax-free)</label>` : "";
  }
  const labelBox = inst => `<label class="fld"><span>Name in the dashboard</span>
      <input class="ipt" id="cf-label" value="${esc(inst.name)}" maxlength="60" /></label>`;

  function renderMethod(box, inst, m) {
    const head = facts(m) + unverified(m, inst) + stepsHtml(m) + docLink(m) + notes(m);
    if (m.support === "live") {
      box.innerHTML = head + `<div>${labelBox(inst)}
        ${(m.fields || []).map(f => `<label class="fld"><span>${esc(f.label)}</span>
          <input class="ipt" data-field="${esc(f.name)}" type="${f.secret ? "password" : "text"}" autocomplete="off"
            autocapitalize="off" spellcheck="false" /></label>`).join("")}
        ${isaBox(inst)}
        <button class="btn" id="cf-go" type="button">Connect</button>
        <div class="msg" id="cf-msg"></div>
        <div class="fine">Read-only: the app can only look, never trade or move money. Your key is checked with
          ${esc(inst.name)} first, then kept on your own server (encrypted in the backup vault) and never shown again.</div></div>`;
      $("cf-go").addEventListener("click", () => connect(inst, m));
    } else if (m.support === "import") {
      const accept = (m.accept || [m.file_format || "csv"]).map(f => "." + f).join(",");
      box.innerHTML = head + `<div class="up"><input type="file" id="cf-file" accept="${esc(accept)}" />
        <button class="btn" id="cf-go" type="button">Upload</button></div><div class="msg" id="cf-msg"></div>`;
      $("cf-go").addEventListener("click", () => importFile(m));
    } else if (m.support === "csv") {
      box.innerHTML = head + `<div>${labelBox(inst)}
        <label class="fld"><span>Currency of the amounts in the file (if it has no currency column)</span>
          <select class="ipt" id="cf-ccy">${["GBP", "EUR", "USD"].map(c => `<option ${c === curDefault() ? "selected" : ""}>${c}</option>`).join("")}</select></label>
        ${isaBox(inst)}
        <div class="up"><input type="file" id="cf-file" accept=".csv,.txt,.tsv" />
          <button class="btn" id="cf-read" type="button">Read file</button></div>
        <div class="msg" id="cf-msg"></div><div id="cf-map"></div></div>`;
      csvFile = null; csvMapping = null;
      $("cf-read").addEventListener("click", () => csvPreview());
      $("cf-ccy").addEventListener("change", () => csvFile && csvPreview(csvMapping));
    } else if (m.support === "manual") {
      box.innerHTML = head + `<div class="sub" style="margin:8px 0">Type in the balance and update it when it changes.
        It takes a few seconds.</div><button class="btn" id="cf-go" type="button">Enter the balance</button>`;
      $("cf-go").addEventListener("click", () => manual(inst));
    } else {
      const why = m.type === "api"
        ? (m.read_only === false ? "This key can also trade or move money, so the app does not use it."
                                 : "This connection is not built into the app yet.")
        : m.type === "upload" ? "The app cannot read this file automatically yet." : "Not available in the app.";
      box.innerHTML = `<div class="note warn">${esc(why)}${esc(others(inst))}</div>` + head;
    }
  }

  function aggregatorHtml(inst, methods) {
    const cards = methods.map(m => {
      const a = m.aggregator || AGG[norm(m.provider)] || {};
      const name = a.name || m.provider || "Bank link service";
      const open = a.available_to_individuals;
      const steps = (a.setup_steps || []).filter(Boolean);
      const covers = [...new Set((m.covers || []).map(c => COVERS[c] || c))].join(", ");
      return `<div class="agg"><b>${esc(name)}</b>
        <span class="pill ${open ? "live" : "bad"}" style="margin-left:6px">${open ? "Free for personal use" : "Not open to individuals"}</span>
        <div class="facts">${covers ? `<span>Would track: <b>${esc(covers)}</b></span>` : ""}
          ${a.consent_renewal_days ? `<span>Reconnect every <b>${esc(a.consent_renewal_days)} days</b></span>` : ""}</div>
        ${a.cost ? `<div class="cmeta">${esc(a.cost)}</div>` : ""}
        ${m.verified === false ? `<div class="cmeta" style="color:#e3b341">Unverified for ${esc(inst.name)}.</div>` : ""}
        ${m.notes ? `<div class="note">${esc(m.notes)}</div>` : ""}
        ${open && steps.length ? `<details><summary class="cmeta" style="cursor:pointer">How it would work</summary>
          <ol class="steps">${steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol></details>` : ""}
        ${docLink(a, name + " docs")}</div>`;
    }).join("");
    return `<div class="note warn">Coming soon. A bank link service connects through your bank's official
      open-banking access and refreshes on its own. It is not built into the app yet.${esc(others(inst))}</div>` + cards;
  }

  function unofficialHtml(inst, methods) {
    return `<div class="note warn">Not offered. These unofficial tools log in as you, with your ${esc(inst.name)}
      username, password or PIN. This app never stores bank or broker logins.${esc(others(inst))}</div>` +
      methods.map(m => `<div class="agg"><b>${esc(m.library || "Unofficial tool")}</b>${notes(m)}</div>`).join("");
  }

  /* ---------------------------------------------------------- actions */
  async function connect(inst, m) {
    const msg = $("cf-msg"), btn = $("cf-go");
    const creds = {};
    $("acct-method").querySelectorAll("[data-field]").forEach(i => { creds[i.dataset.field] = i.value.trim(); });
    const empty = (m.fields || []).filter(f => !creds[f.name]).map(f => f.label);
    if (empty.length) return bad(msg, "Please fill in: " + empty.join(", ") + ".");
    const fd = new FormData();
    fd.append("institution", inst.id); fd.append("method", m.key);
    fd.append("label", $("cf-label").value.trim());
    fd.append("wrapper", $("cf-isa") && $("cf-isa").checked ? "ISA" : "");
    fd.append("credentials", JSON.stringify(creds));
    btn.disabled = true; info(msg, `Checking with ${inst.name}… (this can take up to a minute)`);
    try {
      const r = await fetch("/api/connections", {method: "POST", body: fd});
      if (!r.ok) return bad(msg, await detail(r));
      const out = await r.json(), c = out.connection;
      $("acct-method").querySelectorAll("[data-field]").forEach(i => { i.value = ""; });
      if (out.pending) { msg.textContent = `✓ Saved as “${c.label}”. ${c.error || "The data will appear shortly."}`; msg.className = "msg"; }
      else ok(msg, `✓ Connected “${c.label}”${c.summary ? " — " + c.summary : ""}.` + syncNote());
      afterChange();
    } catch (e) { bad(msg, "Could not reach the server."); }
    finally { btn.disabled = false; }
  }

  async function importFile(m) {
    const inp = $("cf-file"), msg = $("cf-msg");
    if (!inp.files.length) return bad(msg, "Choose a file first.");
    info(msg, "Uploading…");
    const fd = new FormData(); fd.append("file", inp.files[0]);
    const r = await fetch("/api/upload/" + encodeURIComponent(m.upload_kind), {method: "POST", body: fd});
    if (!r.ok) return bad(msg, await detail(r));
    const rep = (await r.json())._report || {};
    let extra = "";
    if (rep.fills !== undefined) extra = ` · ${rep.fills} fills, ${rep.orders} orders`;
    if (rep.trades !== undefined && rep.cash_balance_eur !== undefined) extra = ` · ${rep.trades} trades, ${rep.dividends} dividends`;
    if (rep.all_checks_ok !== undefined) extra = rep.all_checks_ok ? " · all statement checks passed" : " · some statement checks failed";
    ok(msg, savedMsg().replace("Saved", "Uploaded — live now.") + extra);
    afterChange();
  }

  async function csvPreview(mapping) {
    const msg = $("cf-msg"), inp = $("cf-file");
    if (inp && inp.files.length) csvFile = inp.files[0];
    if (!csvFile) return bad(msg, "Choose a file first.");
    const fd = new FormData(); fd.append("file", csvFile);
    fd.append("currency", $("cf-ccy").value);
    if (mapping) fd.append("mapping", JSON.stringify(mapping));
    info(msg, "Reading…");
    const r = await fetch("/api/connections/csv/preview", {method: "POST", body: fd});
    if (!r.ok) { $("cf-map").innerHTML = ""; return bad(msg, await detail(r)); }
    const p = await r.json();
    csvMapping = p.mapping;
    renderMapping(p);
  }

  function renderMapping(p) {
    const msg = $("cf-msg"), cols = p.columns || [];
    const sel = (field) => `<label class="fld"><span>${esc(FIELD_LABEL[field])}</span><select class="ipt" data-map="${field}">
        <option value="">— not in file —</option>${cols.map(c => `<option ${p.mapping[field] === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select></label>`;
    const rows = (p.holdings || []).map(h => `<tr><td>${esc(h.name)}</td><td>${esc(h.ticker || h.isin || "—")}</td>
        <td>${esc(+h.quantity)}</td><td>${esc(h.buy_price)} ${esc(h.cost_currency)}</td><td>${h.price != null ? esc(h.price) : "live"}</td></tr>`).join("");
    $("cf-map").innerHTML = `<div class="sub" style="margin:10px 0 2px">Check which column is which. Fix anything that looks wrong.</div>
      <div class="map">${Object.keys(FIELD_LABEL).map(sel).join("")}
        <label class="fld"><span>The cost column is…</span><select class="ipt" data-map="cost_kind">
          <option value="per_share" ${p.mapping.cost_kind !== "total" ? "selected" : ""}>per share</option>
          <option value="total" ${p.mapping.cost_kind === "total" ? "selected" : ""}>the total paid</option></select></label></div>
      ${rows ? `<div class="pvwrap"><table class="pv"><tr><th>Name</th><th>Symbol</th><th>Qty</th><th>Cost/share</th><th>Price</th></tr>${rows}</table></div>` : ""}
      <button class="btn" id="cf-save" type="button" style="margin-top:10px" ${p.error || !(p.holdings || []).length ? "disabled" : ""}>Add these positions</button>
      <div class="fine">Positions with a symbol get live prices. Without a symbol, the file's own price is used.
        Use Yahoo Finance symbols for non-US shares (for example VWRL.L for London).</div>`;
    const rep = p.report || {};
    if (p.error) bad(msg, p.error);
    else info(msg, `Found ${rep.used} position${rep.used === 1 ? "" : "s"} in ${rep.rows} row${rep.rows === 1 ? "" : "s"}` +
      (rep.skipped ? ` (${rep.skipped} skipped: totals, cash or empty rows)` : "") + ".");
    $("cf-map").querySelectorAll("[data-map]").forEach(s => s.addEventListener("change", () => {
      csvMapping = {...csvMapping, [s.dataset.map]: s.value || null};
      clearTimeout(csvTimer); csvTimer = setTimeout(() => csvPreview(csvMapping), 150);
    }));
    $("cf-save").addEventListener("click", csvSave);
  }

  async function csvSave() {
    const msg = $("cf-msg"), btn = $("cf-save");
    const fd = new FormData();
    fd.append("institution", picked.id); fd.append("method", tab);
    fd.append("label", $("cf-label").value.trim());
    fd.append("wrapper", $("cf-isa") && $("cf-isa").checked ? "ISA" : "");
    fd.append("currency", $("cf-ccy").value);
    fd.append("mapping", JSON.stringify(csvMapping || {}));
    fd.append("file", csvFile);
    btn.disabled = true;
    const r = await fetch("/api/connections", {method: "POST", body: fd});
    btn.disabled = false;
    if (!r.ok) return bad(msg, await detail(r));
    const out = await r.json();
    $("cf-map").innerHTML = "";
    ok(msg, `✓ Added “${out.connection.label}” — ${out.connection.summary}.` + syncNote());
    afterChange();
  }

  function manual(inst) {
    const card = $("cash");
    if (!card) return;
    $("c-inst").value = inst.name;
    if (!$("c-name").value) $("c-name").value = inst.name;
    const type = $("c-type");
    if (type) type.value = ["broker", "robo", "crypto"].includes(inst.kind) ? "other" : "cash";
    card.scrollIntoView({behavior: "smooth", block: "start"});
    card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash");
    setTimeout(() => $("c-bal").focus({preventScroll: true}), 400);
  }

  /* ------------------------------------------------ connected accounts */
  function ago(stamp) {
    if (!stamp) return "not yet";
    const t = typeof stamp === "number" ? stamp * 1000 : Date.parse(stamp);
    if (!t) return "—";
    const s = (Date.now() - t) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return new Date(t).toLocaleDateString("en-GB", {day: "numeric", month: "short", year: "numeric"});
  }
  const STATUS = {ok: ["live", "OK"], error: ["bad", "Error"], pending: ["sample", "Checking"]};

  async function loadConnections() {
    let d;
    try { d = await (await fetch("/api/connections")).json(); }
    catch (e) { $("conn-list").innerHTML = '<span class="err">Could not load connected accounts.</span>'; return; }
    const rows = [];
    for (const c of d.connections || []) {
      const [cls, word] = STATUS[c.status] || STATUS.pending;
      const csv = c.connector === "generic_csv";
      rows.push(`<div class="crow"><div><b>${esc(c.label)}</b>
          ${c.wrapper ? `<span class="pill tag grey">${esc(c.wrapper)}</span>` : ""}
          <span class="pill ${cls}">${word}</span>
          <div class="cmeta">${esc(c.institution_name)}${c.summary ? " · " + esc(c.summary) : ""}</div>
          <div class="cmeta">Updated ${esc(ago(c.last_sync))} · ${esc((c.updates || "").toLowerCase())}</div>
          ${c.error ? `<div class="cerr" ${c.status === "pending" ? 'style="color:#e3b341"' : ""}>${esc(c.error)}</div>` : ""}</div>
        <div class="cact"><button class="btn sm ghost" type="button" data-act="${csv ? "file" : "refresh"}" data-id="${esc(c.id)}">${csv ? "New file" : "Refresh"}</button>
          <button class="del" type="button" title="Remove" aria-label="Remove ${esc(c.label)}" data-act="remove" data-id="${esc(c.id)}" data-label="${esc(c.label)}">✕</button></div></div>`);
    }
    for (const s of d.server || []) {
      const [cls, word] = STATUS[s.status] || STATUS.pending;
      rows.push(`<div class="crow"><div><b>${esc(s.label)}</b> ${s.wrapper ? `<span class="pill tag grey">${esc(s.wrapper)}</span>` : ""}
          <span class="pill ${cls}">${word}</span>
          <div class="cmeta">${esc(s.institution_name)} · key set on the server (.env)${s.summary ? " · " + esc(s.summary) : ""}</div>
          ${s.error ? `<div class="cerr">${esc(s.error)}</div>` : ""}</div></div>`);
    }
    for (const f of d.files || []) {
      rows.push(`<div class="crow"><div><b>${esc(f.label)}</b> <span class="pill live">File</span>
          <div class="cmeta">Uploaded ${esc(ago(f.updated))} · updates when you upload a new file</div></div>
        <div class="cact"><button class="btn sm ghost" type="button" data-act="upload" data-inst="${esc(f.institution)}">New file</button></div></div>`);
    }
    $("conn-list").innerHTML = rows.join("") ||
      '<span style="color:var(--faint)">Nothing connected yet. Add your first account above.</span>';
  }

  let fileFor = null;
  $("conn-list").addEventListener("click", async e => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const id = b.dataset.id;
    if (b.dataset.act === "remove") {
      if (!confirm(`Remove “${b.dataset.label}”? Its key is deleted from this app. You can also delete the key at the bank or broker.`)) return;
      const r = await fetch("/api/connections/" + encodeURIComponent(id), {method: "DELETE"});
      if (!r.ok) alert(await detail(r));
      afterChange();
    } else if (b.dataset.act === "refresh") {
      b.disabled = true; b.textContent = "Checking…";
      await fetch("/api/connections/" + encodeURIComponent(id) + "/refresh", {method: "POST"}).catch(() => {});
      afterChange();
    } else if (b.dataset.act === "file") {
      fileFor = id; $("conn-file").value = ""; $("conn-file").click();
    } else if (b.dataset.act === "upload") {
      const inst = INST.find(i => norm(i.id) === norm(b.dataset.inst));
      if (inst) {
        pick(inst);
        const t = tabsFor(inst).find(t => t.methods.some(m => m.support === "import"));
        if (t) { tab = t.key; renderPanel(); }
        $("add-account").scrollIntoView({behavior: "smooth", block: "start"});
      }
    }
  });
  $("conn-file").addEventListener("change", async () => {
    const f = $("conn-file").files[0]; if (!f || !fileFor) return;
    const fd = new FormData(); fd.append("file", f);
    const r = await fetch("/api/connections/" + encodeURIComponent(fileFor) + "/csv", {method: "POST", body: fd});
    if (!r.ok) alert(await detail(r));
    afterChange();
  });

  loadCatalog();
  loadConnections();
})();
