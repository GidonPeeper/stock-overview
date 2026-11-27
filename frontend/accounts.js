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
      $("acct-panel").innerHTML = '<div class="is-error" style="margin-top:12px"><span>Could not load the list of brokers and banks. <button class="linkbtn" type="button" onclick="location.reload()">Try again</button></span></div>';
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
                                  phone_pin: "Sign in (unofficial)", other: "API"}[m.auth] || "API key";
    return m.type;
  }

  function pick(inst) {
    picked = inst; closeList();
    trRenew = null;             // "Sign in again" sets it after picking
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
    if (t.key === "agg") {
      box.innerHTML = aggregatorHtml(inst, t.methods);
      const link = t.methods.find(isBankLink);
      if (link) bankLink(inst, link);
    }
    else if (t.key === "unofficial") box.innerHTML = unofficialHtml(inst, t.methods);
    else renderMethod(box, inst, t.methods[0]);
  }
  const isBankLink = m => m.support === "live" && m.connector === "enablebanking";
  const isTrLogin = m => m.support === "live" && m.connector === "traderepublic";

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
    if (isTrLogin(m)) return trLogin(box, inst, m);
    const head = facts(m) + unverified(m, inst) + stepsHtml(m) + docLink(m) + notes(m);
    if (m.support === "live") {
      box.innerHTML = head + `<div>${labelBox(inst)}
        ${(m.fields || []).map(f => `<label class="fld"><span>${esc(f.label)}</span>
          <input class="ipt" data-field="${esc(f.name)}" type="${f.secret ? "password" : "text"}" autocomplete="off"
            autocapitalize="off" spellcheck="false" /></label>`).join("")}
        ${isaBox(inst)}
        <div class="btn-row"><button class="btn" id="cf-go" type="button">Connect</button></div>
        <div class="msg" id="cf-msg" role="status"></div>
        <div class="fine">Read-only: the app can only look, never trade or move money. Your key is checked with
          ${esc(inst.name)} first, then saved in the app's data folder (encrypted in the cloud backup, if you use one)
          and never shown again.</div></div>`;
      $("cf-go").addEventListener("click", () => connect(inst, m));
    } else if (m.support === "import") {
      const accept = (m.accept || [m.file_format || "csv"]).map(f => "." + f).join(",");
      box.innerHTML = head + `<div class="up"><input type="file" id="cf-file" accept="${esc(accept)}" />
        <button class="btn" id="cf-go" type="button">Upload</button></div><div class="msg" id="cf-msg" role="status"></div>`;
      $("cf-go").addEventListener("click", () => importFile(m));
    } else if (m.support === "csv") {
      box.innerHTML = head + `<div>${labelBox(inst)}
        <label class="fld"><span>Currency of the amounts in the file (if it has no currency column)</span>
          <select class="ipt" id="cf-ccy">${["GBP", "EUR", "USD"].map(c => `<option ${c === curDefault() ? "selected" : ""}>${c}</option>`).join("")}</select></label>
        ${isaBox(inst)}
        <div class="up"><input type="file" id="cf-file" accept=".csv,.txt,.tsv" />
          <button class="btn" id="cf-read" type="button">Read file</button></div>
        <div class="msg" id="cf-msg" role="status"></div><div id="cf-map"></div></div>`;
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
    const usable = methods.filter(isBankLink);
    const rest = methods.filter(m => !isBankLink(m));
    if (!usable.length) return comingSoon(inst, rest);
    return `<div id="eb-box"><div class="sub" style="margin:10px 0">Loading…</div></div>` +
      (rest.length ? `<details style="margin-top:12px"><summary class="cmeta" style="cursor:pointer">Other bank link
        services (not built into the app)</summary>${aggCards(inst, rest)}</details>` : "");
  }

  function comingSoon(inst, methods) {
    return `<div class="note warn">Coming soon. A bank link service connects through your bank's official
      open-banking access and refreshes on its own. It is not built into the app yet.${esc(others(inst))}</div>` +
      aggCards(inst, methods);
  }

  function aggCards(inst, methods) {
    return methods.map(m => {
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
        ${m.verified === false ? `<div class="cmeta" style="color:var(--amber)">Unverified for ${esc(inst.name)}.</div>` : ""}
        ${m.notes ? `<div class="note">${esc(m.notes)}</div>` : ""}
        ${open && steps.length ? `<details><summary class="cmeta" style="cursor:pointer">How it would work</summary>
          <ol class="steps">${steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol></details>` : ""}
        ${docLink(a, name + " docs")}</div>`;
    }).join("");
  }

  function unofficialHtml(inst, methods) {
    return `<div class="note warn">Not offered. These unofficial tools log in as you, with your ${esc(inst.name)}
      username, password or PIN. This app never stores bank or broker logins.${esc(others(inst))}</div>` +
      methods.map(m => `<div class="agg"><b>${esc(m.library || "Unofficial tool")}</b>${notes(m)}</div>`).join("");
  }

  /* ------------------------------------------- bank link (Enable Banking) */
  // One Enable Banking application (ID + private key) serves every bank. Not
  // set up yet: the setup steps and the two fields. Set up: pick the bank,
  // go there to approve, come back on /enablebanking/callback.
  let ebEdit = false;
  const LINKING = "ebLinking";
  const markLinking = () => { try { localStorage.setItem(LINKING, String(Date.now())); } catch (e) {} };
  const wasLinking = () => { try { return Date.now() - +(localStorage.getItem(LINKING) || 0) < 30 * 60e3; } catch (e) { return false; } };

  async function bankLink(inst, m) {
    const box = $("eb-box"); if (!box) return;
    let st;
    try {
      const r = await fetch("/api/enablebanking");
      if (!r.ok) throw new Error();
      st = await r.json();
    } catch (e) { box.innerHTML = '<div class="msg err">Could not load the bank link settings.</div>'; return; }
    if (!st.configured || ebEdit) ebSetup(box, inst, m, st);
    else ebPicker(box, inst, m, st);
  }

  const httpsWarn = st => st.https ? "" : `<div class="note warn">Enable Banking only sends you back to an https
    address. Open the dashboard at its https address (for example on Render) to link a bank.</div>`;

  function ebSetup(box, inst, m, st) {
    const a = m.aggregator || AGG.enablebanking || {};
    const steps = (a.setup_steps || []).filter(Boolean);
    box.innerHTML = facts(m) + notes(m) +
      `<div class="sub" style="margin:10px 0 0"><b>One-time setup</b> (free, about 10 minutes). One Enable Banking
        application links all your banks.</div>
      <ol class="steps">${steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>
      <label class="fld"><span>Redirect URL to register (copy it exactly)</span>
        <span class="copy"><input class="ipt" id="eb-redirect" readonly value="${esc(st.redirect_url)}" />
        <button class="btn sm ghost" id="eb-copy" type="button">Copy</button></span></label>
      ${httpsWarn(st)}
      ${st.problem ? `<div class="note warn">${esc(st.problem)}</div>` : ""}
      <label class="fld"><span>Application ID</span>
        <input class="ipt" id="eb-app" autocomplete="off" autocapitalize="off" spellcheck="false"
          placeholder="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" /></label>
      <label class="fld"><span>Private key: the .pem file your browser downloaded</span>
        <input type="file" id="eb-keyfile" accept=".pem,.key,.txt" /></label>
      <details><summary class="cmeta" style="cursor:pointer">Or paste the key</summary>
        <textarea class="ipt" id="eb-key" rows="5" autocomplete="off" autocapitalize="off" spellcheck="false"
          placeholder="-----BEGIN PRIVATE KEY-----"></textarea></details>
      <div class="btn-row"><button class="btn" id="eb-save" type="button">Save and check</button>
        ${st.configured ? '<button class="btn ghost" id="eb-cancel" type="button">Cancel</button>' : ""}
        ${st.source === "app" ? '<button class="btn danger" id="eb-forget" type="button">Remove the saved key</button>' : ""}</div>
      <div class="msg" id="eb-msg" role="status"></div>
      <div class="fine">The key is checked with Enable Banking, then saved in the app's data folder (encrypted in the
        cloud backup, if you use one) and never shown again. It can only read the accounts you link.</div>
      ${docLink(a, "Enable Banking docs")}`;
    $("eb-copy").addEventListener("click", () => {
      const i = $("eb-redirect"); i.select();
      (navigator.clipboard ? navigator.clipboard.writeText(i.value) : Promise.reject()).then(
        () => { $("eb-copy").textContent = "Copied"; }, () => { try { document.execCommand("copy"); } catch (e) {} });
    });
    $("eb-keyfile").addEventListener("change", () => {
      // the .pem file is named after the application ID
      const f = $("eb-keyfile").files[0], id = $("eb-app");
      const hit = f && /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(f.name);
      if (hit && !id.value.trim()) id.value = hit[1];
    });
    if ($("eb-cancel")) $("eb-cancel").addEventListener("click", () => { ebEdit = false; bankLink(inst, m); });
    if ($("eb-forget")) $("eb-forget").addEventListener("click", async () => {
      if (!confirm("Remove the Enable Banking key from this app? Linked banks stop updating until you add it again.")) return;
      const r = await fetch("/api/enablebanking/app", {method: "DELETE"}).catch(() => null);
      if (!r || !r.ok) return bad($("eb-msg"), r ? await detail(r) : "Could not reach the server.");
      ebEdit = false; bankLink(inst, m);
    });
    $("eb-save").addEventListener("click", () => ebSave(inst, m));
  }

  async function ebSave(inst, m) {
    const msg = $("eb-msg"), btn = $("eb-save");
    const appId = $("eb-app").value.trim(), file = $("eb-keyfile").files[0], pasted = $("eb-key").value.trim();
    if (!appId) return bad(msg, "Fill in the application ID.");
    if (!file && !pasted) return bad(msg, "Choose the .pem file, or paste the key.");
    const fd = new FormData();
    fd.append("app_id", appId);
    if (pasted) fd.append("private_key", pasted); else fd.append("key_file", file);
    btn.disabled = true; info(msg, "Checking with Enable Banking…");
    try {
      const r = await fetch("/api/enablebanking/app", {method: "POST", body: fd});
      if (!r.ok) return bad(msg, await detail(r));
      ebEdit = false;
      await bankLink(inst, m);
    } catch (e) { bad(msg, "Could not reach the server."); }
    finally { if (document.body.contains(btn)) btn.disabled = false; }
  }

  function ebPicker(box, inst, m, st) {
    box.innerHTML = facts(m) + notes(m) +
      `<div class="cmeta" style="margin:6px 0">Enable Banking application ${esc(st.app_id)} ·
        ${st.source === "env" ? "set on the server" : "saved in the app"} ·
        <a href="#" id="eb-change" class="doc" style="font-size:inherit">change</a></div>
      ${httpsWarn(st)}
      <div class="map" style="grid-template-columns:minmax(90px,120px) 1fr">
        <label class="fld"><span>Country</span><select class="ipt" id="eb-country"></select></label>
        <label class="fld"><span>Bank</span><select class="ipt" id="eb-bank"><option>Loading…</option></select></label>
      </div>
      ${labelBox(inst)}
      <div class="btn-row"><button class="btn" id="eb-go" type="button" disabled>Link</button></div>
      <div class="msg" id="eb-msg" role="status"></div>
      <div class="fine">You go to your bank to approve read-only access to your balances, then come back here.
        On iPhone the bank may finish in Safari: then just open this app again. Access lasts up to the bank's maximum
        (often 180 days); the app reminds you to renew it.</div>`;
    $("eb-change").addEventListener("click", e => { e.preventDefault(); ebEdit = true; bankLink(inst, m); });
    $("eb-country").addEventListener("change", () => ebBanks(inst, $("eb-country").value));
    $("eb-bank").addEventListener("change", ebButton);
    $("eb-go").addEventListener("click", () => ebStart(inst, m));
    ebBanks(inst, "");
  }

  async function ebBanks(inst, country) {
    const msg = $("eb-msg"), sel = $("eb-bank");
    sel.innerHTML = "<option>Loading…</option>"; $("eb-go").disabled = true;
    let d;
    try {
      const r = await fetch(`/api/enablebanking/banks?institution=${encodeURIComponent(inst.id)}&country=${encodeURIComponent(country)}`);
      if (!r.ok) { sel.innerHTML = ""; return bad(msg, await detail(r)); }
      d = await r.json();
    } catch (e) { sel.innerHTML = ""; return bad(msg, "Could not reach the server."); }
    $("eb-country").innerHTML = (d.countries || []).map(c =>
      `<option ${c === d.country ? "selected" : ""}>${esc(c)}</option>`).join("");
    sel.innerHTML = (d.banks || []).length
      ? d.banks.map(b => `<option value="${esc(b.name)}" data-days="${esc(b.max_days || "")}" ${b.name === d.suggested ? "selected" : ""}>${esc(b.name)}${b.beta ? " (beta)" : ""}</option>`).join("")
      : "<option value=''>No banks in this country</option>";
    if (!d.suggested && (d.banks || []).length) sel.insertAdjacentHTML("afterbegin", "<option value='' selected>Choose your bank…</option>");
    info(msg, d.suggested || !(d.banks || []).length ? "" : `${inst.name} was not found in this country's list. Pick it, or another country.`);
    ebButton();
  }

  function ebButton() {
    const sel = $("eb-bank"), btn = $("eb-go");
    btn.disabled = !sel.value;
    btn.textContent = sel.value ? `Link ${sel.value}` : "Link";
  }

  async function ebStart(inst, m) {
    const msg = $("eb-msg"), btn = $("eb-go");
    const fd = new FormData();
    fd.append("institution", inst.id); fd.append("method", m.key);
    fd.append("aspsp", $("eb-bank").value); fd.append("country", $("eb-country").value);
    fd.append("label", $("cf-label").value.trim());
    btn.disabled = true; info(msg, `Opening ${$("eb-bank").value}…`);
    await goToBank(fd, msg);
    btn.disabled = false;
  }

  async function goToBank(fd, msg) {
    try {
      const r = await fetch("/api/enablebanking/start", {method: "POST", body: fd});
      if (!r.ok) { const t = await detail(r); if (msg) bad(msg, t); else alert(t); return; }
      const out = await r.json();
      markLinking();
      location.href = out.url;
    } catch (e) { if (msg) bad(msg, "Could not reach the server."); else alert("Could not reach the server."); }
  }

  // Back from the bank (the PWA was only in the background): show the new link.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && wasLinking()) afterChange();
  });

  /* --------------------------------------------- Trade Republic sign-in */
  // Phone number + PIN, then approve in the Trade Republic app (or enter a code).
  // The PIN goes to the server once and is cleared from the page at once.
  let trRenew = null;     // {id, label}: the connection "Sign in again" renews
  let trState = null;     // the sign-in in progress: {id, step, until}
  let trTimer = null;

  function trLogin(box, inst, m) {
    const renew = trRenew;
    box.innerHTML = facts(m) + stepsHtml(m) +
      `<div class="note warn">Unofficial: Trade Republic has no public API. This signs in the way Trade Republic's
        own web app does, like the open-source tool pytr. It may stop working when Trade Republic changes its app,
        and using it may be against Trade Republic's terms. You decide.</div>
      <div id="tr-form" class="tr-box">
        <div class="step-h"><span class="step-n" aria-hidden="true">1</span>Your phone number and PIN</div>
        ${renew ? `<div class="sub" style="margin:8px 0">Signing in again for “${esc(renew.label)}”.</div>` : labelBox(inst)}
        <label class="fld"><span>Phone number, with the country code</span>
          <input class="ipt" id="tr-phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="+49 170 1234567" /></label>
        <label class="fld"><span>PIN</span>
          <input class="ipt" id="tr-pin" type="password" inputmode="numeric" autocomplete="off" /></label>
        <div class="btn-row"><button class="btn" id="tr-go" type="button">Sign in</button></div>
      </div>
      <div id="tr-step"></div>
      <div class="msg" id="tr-msg" role="status"></div>
      <div class="fine">Your PIN goes to Trade Republic once, to start the sign-in, and is never stored. The app keeps
        only the sign-in session, in the app's data folder (encrypted in the cloud backup, if you use one). It can only read: it has no way
        to trade, move money or change settings.</div>` + docLink(m, "pytr, the open-source tool this follows");
    $("tr-go").addEventListener("click", () => trStart(inst, m, renew));
    $("tr-pin").addEventListener("keydown", e => { if (e.key === "Enter") trStart(inst, m, renew); });
    if (trState) trCancel(true);
  }

  async function trStart(inst, m, renew) {
    const msg = $("tr-msg"), btn = $("tr-go"), pin = $("tr-pin");
    const phone = $("tr-phone").value.trim();
    if (!phone) return bad(msg, "Enter your phone number.");
    if (!pin.value) return bad(msg, "Enter your PIN.");
    const fd = new FormData();
    fd.append("institution", inst.id); fd.append("method", m.key); fd.append("phone", phone);
    fd.append("pin", pin.value);
    fd.append("label", $("cf-label") ? $("cf-label").value.trim() : "");
    if (renew) fd.append("renew", renew.id);
    pin.value = "";
    btn.disabled = true; info(msg, "Contacting Trade Republic…");
    try {
      const r = await fetch("/api/traderepublic/login", {method: "POST", body: fd});
      if (!r.ok) return bad(msg, await detail(r));
      trShow(await r.json());
    } catch (e) { bad(msg, "Could not reach the server."); }
    finally { if (document.body.contains(btn)) btn.disabled = false; }
  }

  function trShow(s) {
    trState = {id: s.login, step: s.step, until: Date.now() + (s.expires_in || 120) * 1000};
    $("tr-form").hidden = true;
    info($("tr-msg"), "");
    const box = $("tr-step");
    if (s.step === "app") {
      box.innerHTML = `<div class="tr-box"><div class="step-h"><span class="step-n" aria-hidden="true">2</span>Approve in the Trade Republic app</div>
        <div class="note"><b>Check your phone.</b> ${esc(s.message)}
          <span class="cmeta" id="tr-left"></span></div>
        <div class="btn-row"><button class="btn sm ghost" id="tr-cancel" type="button">Cancel</button></div></div>`;
      setTimeout(trPoll, 1500);
    } else {
      const n = s.step === "code" ? 4 : 6;
      box.innerHTML = `<div class="tr-box"><div class="step-h"><span class="step-n" aria-hidden="true">2</span>Enter the code</div>
        <label class="fld"><span>${esc(s.message)}</span>
          <input class="ipt" id="tr-code" inputmode="numeric" autocomplete="one-time-code" maxlength="${n}"
            placeholder="${"0".repeat(n)}" style="max-width:220px;letter-spacing:.2em" /></label>
        <div class="btn-row"><button class="btn" id="tr-ok" type="button">Confirm</button>
        ${s.can_resend ? '<button class="btn ghost" id="tr-sms" type="button">Send it by SMS</button>' : ""}
        <button class="btn ghost" id="tr-cancel" type="button">Cancel</button>
        <span class="cmeta" id="tr-left"></span></div></div>`;
      const go = () => trConfirm($("tr-code").value.trim());
      $("tr-ok").addEventListener("click", go);
      $("tr-code").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
      if ($("tr-sms")) $("tr-sms").addEventListener("click", trResend);
      $("tr-code").focus();
    }
    $("tr-cancel").addEventListener("click", () => trCancel(false));
    clearInterval(trTimer);
    trTimer = setInterval(trTick, 1000); trTick();
  }

  function trTick() {
    const el = $("tr-left");
    if (!trState || !el) return clearInterval(trTimer);
    const s = Math.max(0, Math.round((trState.until - Date.now()) / 1000));
    el.textContent = `(${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} left)`;
  }

  function trPoll() { if (trState && trState.step === "app") trConfirm(""); }

  async function trConfirm(code) {
    const st = trState; if (!st) return;
    const msg = $("tr-msg"), btn = $("tr-ok");
    if (Date.now() > st.until) { trReset(); return bad(msg, "The sign-in expired. Start again."); }
    const fd = new FormData(); if (code) fd.append("code", code);
    if (btn) btn.disabled = true;
    if (code) info(msg, "Checking…");
    let r;
    try {
      r = await fetch(`/api/traderepublic/login/${encodeURIComponent(st.id)}/confirm`, {method: "POST", body: fd});
    } catch (e) {
      if (trState === st && st.step === "app") return setTimeout(trPoll, 3000);   // a network blip: keep waiting
      if (btn && document.body.contains(btn)) btn.disabled = false;
      return bad(msg, "Could not reach the server.");
    }
    if (trState !== st) return;                        // cancelled meanwhile
    if (btn && document.body.contains(btn)) btn.disabled = false;
    if (!r.ok) {
      const text = await detail(r);
      if (r.status === 404 || /start again/i.test(text)) trReset();
      return bad(msg, text);
    }
    const out = await r.json();
    if (out.status === "done") return trDone(out.connection);
    if (out.step !== st.step) return trShow(out);      // e.g. code accepted: now approve in the app
    if (out.step === "app") setTimeout(trPoll, 500);
  }

  async function trResend() {
    const st = trState; if (!st) return;
    const r = await fetch(`/api/traderepublic/login/${encodeURIComponent(st.id)}/resend`, {method: "POST"}).catch(() => null);
    if (!r) return bad($("tr-msg"), "Could not reach the server.");
    if (!r.ok) return bad($("tr-msg"), await detail(r));
    info($("tr-msg"), "Trade Republic is sending the code by SMS.");
  }

  function trReset() {
    trState = null; clearInterval(trTimer);
    if ($("tr-step")) $("tr-step").innerHTML = "";
    if ($("tr-form")) $("tr-form").hidden = false;
  }

  function trCancel(quiet) {
    const st = trState; trReset();
    if (st) fetch(`/api/traderepublic/login/${encodeURIComponent(st.id)}`, {method: "DELETE"}).catch(() => {});
    if (!quiet && $("tr-msg")) info($("tr-msg"), "Sign-in cancelled.");
  }

  function trDone(c) {
    trReset(); trRenew = null;
    const found = String(c.summary || "").split(" · ")[0];
    ok($("tr-msg"), `✓ Connected “${c.label}”${found ? " — " + found : ""}. Your history is read in the background; ` +
      "gains and dividends appear within a few minutes." + syncNote());
    afterChange();
  }

  // "Sign in again" on a connection whose Trade Republic session has ended.
  function trSignInAgain(id, label) {
    const inst = INST.find(i => norm(i.id) === "traderepublic");
    if (!inst) return;
    pick(inst);
    trRenew = {id, label};
    const t = tabsFor(inst).find(t => t.methods.some(isTrLogin));
    if (t) { tab = t.key; renderPanel(); }
    $("add-account").scrollIntoView({behavior: "smooth", block: "start"});
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
  const STATUS = {ok: ["live", "OK"], error: ["bad", "Error"], pending: ["sample", "Checking"],
                  signin: ["sample", "Signed out"]};
  const day = iso => new Date(iso + "T12:00:00Z").toLocaleDateString("en-GB", {day: "numeric", month: "short", year: "numeric"});
  const days = n => `${n} day${n === 1 ? "" : "s"}`;

  // A bank link's consent: "renew in N days"; a warning from 14 days before.
  function consentHtml(c) {
    const k = c.consent; if (!k) return "";
    if (k.expired) return c.error ? "" : `<div class="cerr">Bank access ended on ${esc(day(k.expires))}. Press Renew to link it again.</div>`;
    if (k.warn) return `<div class="cerr" style="color:var(--amber)">Bank access ends ${k.days_left ? "in " + esc(days(k.days_left)) : "today"}
      (${esc(day(k.expires))}). Press Renew to keep it updating.</div>`;
    return `<div class="cmeta">Renew in ${esc(days(k.days_left))} (bank access until ${esc(day(k.expires))})</div>`;
  }

  // The same bank also typed in by hand would be counted twice.
  function dupHtml(c) {
    const names = c.manual_duplicates || [];
    if (!names.length) return "";
    return `<div class="note" style="margin:6px 0 0">You also typed in ${names.map(n => `“${esc(n)}”`).join(", ")} as a balance.
      If it is the same account, remove it under <a href="#cash" class="doc" style="font-size:inherit">Bank, savings &amp;
      retirement accounts</a> so it is not counted twice.</div>`;
  }

  async function loadConnections() {
    let d;
    try { d = await (await fetch("/api/connections")).json(); }
    catch (e) { $("conn-list").innerHTML = '<div class="is-error"><span>Could not load connected accounts. <button class="linkbtn" type="button" onclick="location.reload()">Try again</button></span></div>'; return; }
    const rows = [];
    for (const c of d.connections || []) {
      const [cls, word] = STATUS[c.status] || STATUS.pending;
      const csv = c.connector === "generic_csv", link = c.connector === "enablebanking";
      const tr = c.connector === "traderepublic", signin = c.status === "signin";
      const bank = c.bank || {};
      const via = link ? ` via Enable Banking${bank.aspsp && norm(bank.aspsp) !== norm(c.institution_name) ? ` (${bank.aspsp})` : ""}`
        : tr ? ` · sign-in (unofficial)${c.tr && c.tr.phone ? ", phone " + c.tr.phone : ""}` : "";
      rows.push(`<div class="crow"><div><b>${esc(c.label)}</b>
          ${c.wrapper ? `<span class="pill tag grey">${esc(c.wrapper)}</span>` : ""}
          <span class="pill ${cls}">${word}</span>
          <div class="cmeta">${esc(c.institution_name)}${esc(via)}${c.summary ? " · " + esc(c.summary) : ""}</div>
          <div class="cmeta">Updated ${esc(ago(c.last_sync))} · ${esc((c.updates || "").toLowerCase())}</div>
          ${consentHtml(c)}
          ${c.error ? `<div class="cerr" ${c.status === "pending" || signin ? 'style="color:var(--amber)"' : ""}>${esc(c.error)}</div>` : ""}
          ${dupHtml(c)}</div>
        <div class="cact">${signin ? `<button class="btn sm" type="button" data-act="tr-signin" data-id="${esc(c.id)}" data-label="${esc(c.label)}">Sign in again</button>`
            : `<button class="btn sm ghost" type="button" data-act="${csv ? "file" : "refresh"}" data-id="${esc(c.id)}">${csv ? "New file" : "Refresh"}</button>`}
          ${link ? `<button class="btn sm ${c.consent && (c.consent.warn || c.consent.expired) ? "" : "ghost"}" type="button" data-act="renew" data-id="${esc(c.id)}">Renew</button>` : ""}
          <button class="del" type="button" title="Remove" aria-label="Remove ${esc(c.label)}" data-act="remove" data-id="${esc(c.id)}" data-label="${esc(c.label)}" ${link ? 'data-link="1"' : ""} ${tr ? 'data-tr="1"' : ""} ${csv ? 'data-csv="1"' : ""}>✕</button></div></div>`);
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
          <div class="cmeta">Uploaded ${esc(ago(f.updated))} · updates when you upload a new file</div>
          ${f.note ? `<div class="cmeta">${esc(f.note)}</div>` : ""}</div>
        <div class="cact"><button class="btn sm ghost" type="button" data-act="upload" data-inst="${esc(f.institution)}">New file</button></div></div>`);
    }
    $("conn-list").innerHTML = rows.join("") ||
      '<div class="empty">Nothing connected yet. Add your first account above.</div>';
  }

  let fileFor = null;
  $("conn-list").addEventListener("click", async e => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const id = b.dataset.id;
    if (b.dataset.act === "tr-signin") {
      trSignInAgain(id, b.dataset.label);
    } else if (b.dataset.act === "remove") {
      const what = b.dataset.link ? "The bank link is ended and removed from this app."
        : b.dataset.tr ? "The saved Trade Republic sign-in and the history read so far are deleted from this app."
        : b.dataset.csv ? "Its positions are removed from this app. Your file is not touched."
        : "Its key is deleted from this app. You can also delete the key at the bank or broker.";
      if (!confirm(`Remove “${b.dataset.label}”? ${what}`)) return;
      const r = await fetch("/api/connections/" + encodeURIComponent(id), {method: "DELETE"});
      if (!r.ok) alert(await detail(r));
      afterChange();
    } else if (b.dataset.act === "renew") {
      b.disabled = true; b.textContent = "Opening…";
      const fd = new FormData(); fd.append("renew", id);
      await goToBank(fd, null);
      b.disabled = false; b.textContent = "Renew";
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
