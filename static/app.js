/* Outreach dashboard front-end. Plain JavaScript, no build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const main = $("#main");
const MAX_BATCH = 30;
let pollTimer = null;

// ------------------------------------------------------------------ helpers

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    let msg = data && data.detail ? data.detail : `Request failed (${res.status})`;
    if (Array.isArray(msg)) msg = msg.map((m) => m.msg).join("; ");
    throw new Error(msg);
  }
  return data;
}

function toast(message, bad = false) {
  const t = $("#toast");
  t.textContent = message;
  t.className = "toast" + (bad ? " bad" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), bad ? 7000 : 3500);
}

// Locally Google opens in a new browser window; on the hosted app this page goes to Google and comes back.
async function connectGmail() {
  const r = await api("/api/gmail/connect", { method: "POST" });
  if (r && r.auth_url) { window.location.href = r.auth_url; return true; }
  return false;
}

async function run(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); return null; }
}

function openModal(title, html) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); $("#modalBody").innerHTML = ""; }
$("#modalClose").onclick = closeModal;
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

const PILL = {
  // approval
  pending: "warn", approved: "ok", rejected: "bad",
  // draft
  generated: "", edited: "blue", cancelled: "",
  // sending
  not_sent: "", queued: "blue", sending: "blue", sent: "ok", failed: "bad", uncertain: "bad", skipped: "warn", simulated: "blue",
  // contact
  new: "", drafted: "blue", contacted: "ok", replied: "ok", opted_out: "bad", do_not_contact: "bad", bounced: "bad", not_interested: "warn",
  // batch
  awaiting_confirmation: "warn", completed: "ok", stopped: "bad",
};
function pill(status, label) {
  return `<span class="pill ${PILL[status] ?? ""}">${esc(label || String(status).replace(/_/g, " "))}</span>`;
}

function selectedIds(root = main) {
  return $$("input.row-check:checked", root).map((c) => Number(c.value));
}

function wireSelectAll(root = main) {
  const all = $("input.check-all", root);
  if (all) all.onchange = () => $$("input.row-check", root).forEach((c) => { if (!c.disabled) c.checked = all.checked; });
}

function emptyRow(cols, text) {
  return `<tr><td colspan="${cols}" class="empty">${esc(text)}</td></tr>`;
}

// ------------------------------------------------------------------ router

const pages = {};

async function route() {
  clearInterval(pollTimer);
  const page = (location.hash || "#overview").slice(1);
  $$(".sidebar a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  $("#sidebar").classList.remove("open");
  main.innerHTML = `<p class="muted">Loading…</p>`;
  await run(() => (pages[page] || pages.overview)());
  refreshModeBadge();
  if (page !== "followups") refreshAlerts();
}
window.addEventListener("hashchange", route);
api("/api/meta").then((m) => { if (m && m.login_enabled) $("#logoutLink").classList.remove("hidden"); }).catch(() => {});
$("#menuBtn").onclick = () => $("#sidebar").classList.toggle("open");

async function refreshModeBadge() {
  const s = await api("/api/stats").catch(() => null);
  if (!s) return;
  const label = { dry_run: ["Dry run", "blue"], test: ["Test mode", "warn"], live: ["LIVE sending", "bad"] }[s.mode];
  $("#modeBadge").innerHTML = `<span class="pill ${label[1]}">${label[0]}</span>`;
}

function modeBanner(mode, testRecipient) {
  if (mode === "dry_run") return `<div class="banner info"><b>Dry run mode.</b> No real emails will be sent. Change the provider in <a href="#settings">Settings</a> when you're ready.</div>`;
  if (mode === "test") return `<div class="banner warn"><b>Test mode.</b> Every email goes only to your own address${testRecipient ? ` (<b>${esc(testRecipient)}</b>)` : ""}, never to the real recipients.</div>`;
  return `<div class="banner bad"><b>Live mode.</b> Confirmed emails will be sent to the real recipients.</div>`;
}

// ------------------------------------------------------------------ alerts (replies, bounces, follow-ups due)

const ALERT_LABEL = { reply: ["Reply", "ok"], bounce: ["Bounced", "bad"], followup_due: ["Follow-up due", "warn"] };
let lastAlertId = null;
try { lastAlertId = Number(localStorage.getItem("lastAlertId")) || null; } catch (_) { /* storage blocked */ }

async function refreshAlerts() {
  const n = await api("/api/notifications").catch(() => null);
  if (!n) return null;
  $$(".bell-count").forEach((el) => { el.textContent = n.unread; el.classList.toggle("hidden", !n.unread); });
  const nav = $("#navAlertCount");
  if (nav) { nav.textContent = n.unread; nav.classList.toggle("hidden", !n.unread); }
  const newest = n.items.length ? n.items[0].id : 0;
  if (lastAlertId !== null && newest > lastAlertId && "Notification" in window && Notification.permission === "granted") {
    n.items.filter((x) => x.id > lastAlertId && !x.read_at).slice(0, 3).forEach((x) => {
      const [label] = ALERT_LABEL[x.kind] || [x.kind];
      try { new Notification(`${label}: ${x.website_name || x.email}`, { body: x.subject || x.from_addr || "" }); } catch (_) { /* ignore */ }
    });
  }
  lastAlertId = newest;
  try { localStorage.setItem("lastAlertId", String(newest)); } catch (_) { /* ignore */ }
  return n;
}
setInterval(refreshAlerts, 60000);

async function showReply(id) {
  openModal("Loading reply…", `<p class="muted">Loading the message from Gmail…</p>`);
  const r = await api(`/api/notifications/${id}/message`).catch((e) => ({ error: e.message }));
  if (r.error) { $("#modalBody").innerHTML = `<div class="banner bad">${esc(r.error)}</div>`; return; }
  const who = r.website_name || r.email;
  const pre = (t) => `<div style="white-space:pre-wrap;border:1px solid var(--border);border-radius:8px;padding:12px;max-height:340px;overflow:auto;background:var(--surface)">${esc(t)}</div>`;
  $("#modalTitle").textContent = `${r.kind === "bounce" ? "Bounce" : "Reply"} from ${who}`;
  $("#modalBody").innerHTML = `
    <div class="meta-row"><b>From:</b> ${esc(r.from)} · <b>Received:</b> ${fmtDate(r.received_at)}</div>
    <div class="meta-row"><b>Subject:</b> ${esc(r.subject)}</div>
    <div class="meta-row small muted">${esc(r.action || "")}</div>
    ${r.needs_permission || r.message ? `<div class="banner ${r.needs_permission ? "info" : "warn"}">${esc(r.message)}
      ${r.needs_permission ? `<div style="margin-top:8px"><button class="btn primary" id="allowTextModal">Allow reading replies</button>
        <span class="small">Opens Google in your browser. Read-only: can't delete, change or send anything.</span></div>` : ""}</div>` : ""}
    ${r.text ? `<h4>${r.kind === "bounce" ? "Bounce message" : "Their reply"}</h4>${pre(r.reply_only || r.text)}
      ${r.reply_only && r.reply_only !== r.text ? `<details><summary class="small">Show the full email, including quoted earlier messages</summary>${pre(r.text)}</details>` : ""}` : ""}
    ${r.yours ? `<details ${r.text ? "" : "open"}><summary><b>The email you sent</b> <span class="small muted">${fmtDate(r.yours.sent_at)} · ${esc(r.yours.subject)}</span></summary>${pre(r.yours.body)}</details>` : ""}
    <div class="toolbar" style="margin-top:12px">
      ${r.gmail_url ? `<a class="btn primary" href="${esc(r.gmail_url)}" target="_blank" rel="noopener noreferrer">Open in Gmail to reply</a>` : ""}
      <button class="btn" onclick="closeModal()">Close</button></div>`;
  const am = $("#allowTextModal");
  if (am) am.onclick = () => run(async () => {
    if (await connectGmail()) return;
    am.disabled = true; am.textContent = "Waiting for Google…";
    toast("A Google window opened in your browser. Approve read-only access, then click View reply again.");
  });
  refreshAlerts();
}

// Alerts now live on the Follow-ups page; old #alerts links still work.
pages.alerts = async () => { location.hash = "#followups"; };

async function alertsSection() {
  const n = await refreshAlerts() || await api("/api/notifications");
  const gm = await api("/api/gmail/status").catch(() => ({}));
  const lc = n.last_check;
  const counts = n.items.reduce((a, x) => ((a[x.kind] = (a[x.kind] || 0) + 1), a), {});
  const html = `
    <div class="page-head" id="alertsSection">
      <div><h2 style="margin:0">&#128276; Replies &amp; alerts</h2><p class="lead">Replies, bounces and follow-ups that are due. The app checks Gmail every
        ${n.interval_minutes} minutes while it's open, reading only the sender, subject and date of messages, never their text.</p></div>
      <button class="btn primary" id="checkNow" ${n.ready ? "" : "disabled"}>Check replies now</button>
    </div>
    ${!n.ready ? `<div class="banner warn"><b>Reply checking is off.</b> ${esc(n.problem)}
      ${gm.connected ? `<div style="margin-top:8px"><button class="btn primary" id="allowReplies">Allow reply checking</button>
        <span class="small">Opens Google in your browser. Sending keeps working the same way.</span></div>` :
        `<div style="margin-top:8px"><a class="btn" href="#settings">Open Settings to connect Gmail</a></div>`}</div>` : ""}
    ${n.ready && !n.reply_text ? `<div class="banner info"><b>See what clients wrote, here in the app.</b> Replies are detected, but showing their text
      needs read-only Gmail access. The app opens only messages it identified as replies to your outreach, and only when you click View reply.
      Read-only access can't delete, change or send anything.
      <div style="margin-top:8px"><button class="btn primary" id="allowText">Allow reading replies</button>
      <span class="small">Opens Google in your browser. Sending keeps working the same way.</span></div></div>` : ""}
    <div class="cards">
      <div class="card stat ${n.unread ? "warn" : ""}"><div class="num">${n.unread}</div><div class="label">Unread alerts</div></div>
      <div class="card stat ok"><div class="num">${counts.reply || 0}</div><div class="label">Replies</div></div>
      <div class="card stat ${counts.bounce ? "bad" : ""}"><div class="num">${counts.bounce || 0}</div><div class="label">Bounces</div></div>
      <div class="card stat"><div class="num">${counts.followup_due || 0}</div><div class="label">Follow-ups due</div></div>
    </div>
    <div class="card small">
      ${lc ? `Last check: <b>${fmtDate(lc.at)}</b> (${esc(lc.trigger)}). ${esc(lc.message)}${(lc.errors || []).length ? ` <span class="muted">${lc.errors.length} warning(s).</span>` : ""}` : "No reply check has run yet."}
      <div class="toolbar" style="margin-top:8px">
        <button class="btn sm" id="readAll" ${n.unread ? "" : "disabled"}>Mark all as read</button>
        ${"Notification" in window ? `<button class="btn sm" id="desktopNotify">${Notification.permission === "granted" ? "Browser pop-ups are on" : "Turn on browser pop-ups"}</button>` : ""}
      </div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Type</th><th>Website / contact</th><th>From</th><th>Subject and preview</th><th>When</th><th>What the app did</th><th></th></tr></thead>
      <tbody>${n.items.length ? n.items.map((x) => {
        const [label, cls] = ALERT_LABEL[x.kind] || [x.kind, ""];
        return `<tr class="alert-row ${x.read_at ? "" : "unread"}">
          <td><span class="pill ${cls}">${esc(label)}</span>${x.read_at ? "" : ` <b class="small">new</b>`}</td>
          <td class="wrap"><b>${esc(x.website_name || "")}</b><br><span class="small">${esc(x.email)}</span></td>
          <td class="small wrap">${esc(x.from_addr || "")}</td>
          <td class="wrap"><b>${esc(x.subject || "")}</b>${x.snippet ? `<br><span class="small muted">${esc(x.snippet)}</span>` : ""}</td>
          <td class="small">${fmtDate(x.received_at || x.created_at)}</td>
          <td class="small wrap">${esc(x.action || "")}</td>
          <td>${x.kind === "followup_due" ? `<button class="btn sm" data-goto-drafts>Review</button>` : `<button class="btn sm primary" data-viewreply="${x.id}">View ${x.kind === "bounce" ? "bounce" : "reply"}</button>
            <a class="btn sm" href="https://mail.google.com/mail/u/0/#all/${encodeURIComponent(x.key)}" target="_blank" rel="noopener noreferrer">Open in Gmail</a>`}
            ${x.read_at ? "" : `<button class="btn sm" data-read="${x.id}">Mark read</button>`}</td></tr>`;
      }).join("") : emptyRow(7, n.ready ? "No replies, bounces or due follow-ups yet." : "No alerts yet. Turn on reply checking above.")}</tbody>
    </table></div>`;

  const wire = () => {
  $$("[data-goto-drafts]").forEach((b) => (b.onclick = () => { const t = $("#fdrafts"); if (t) t.scrollIntoView({ behavior: "smooth" }); }));
  const ck = $("#checkNow");
  if (ck) ck.onclick = () => run(async () => {
    ck.disabled = true; ck.textContent = "Checking…";
    const r = await api("/api/notifications/check", { method: "POST" });
    toast(r.message, !r.checked);
    route();
  });
  $$("[data-viewreply]").forEach((b) => (b.onclick = () => showReply(Number(b.dataset.viewreply))));
  const allowText = $("#allowText");
  if (allowText) allowText.onclick = () => run(async () => {
    if (await connectGmail()) return;
    toast("A Google window opened in your browser. Approve read-only access, then come back and click View reply.");
    setTimeout(route, 8000);
  });
  const allow = $("#allowReplies");
  if (allow) allow.onclick = () => run(async () => {
    if (await connectGmail()) return;
    toast("A Google window opened in your browser. Approve access, then come back and click Check replies now.");
    setTimeout(route, 8000);
  });
  const ra = $("#readAll");
  if (ra) ra.onclick = () => run(async () => { await api("/api/notifications/read", { method: "POST", body: { ids: [] } }); route(); });
  $$("[data-read]").forEach((b) => (b.onclick = () => run(async () => {
    await api("/api/notifications/read", { method: "POST", body: { ids: [Number(b.dataset.read)] } }); route();
  })));
  const dn = $("#desktopNotify");
  if (dn) dn.onclick = () => run(async () => {
    const p = await Notification.requestPermission();
    toast(p === "granted" ? "Browser pop-ups are on while this page is open." : "Your browser blocked pop-ups for this page.", p !== "granted");
    route();
  });
  };
  return { html, wire };
}

// ------------------------------------------------------------------ 1. overview

pages.overview = async () => {
  const [s, d, al] = await Promise.all([api("/api/stats"), api("/api/discovery/status").catch(() => null),
    api("/api/notifications").catch(() => null)]);
  const card = (num, label, cls = "") => `<div class="card stat ${cls}"><div class="num">${num}</div><div class="label">${label}</div></div>`;
  const unreadReplies = al ? al.items.filter((x) => !x.read_at && x.kind === "reply").length : 0;
  main.innerHTML = `
    <h1>Dashboard</h1>
    <p class="lead">Your outreach at a glance. Nothing is ever sent without your approval and a final confirmation.</p>
    ${modeBanner(s.mode)}
    ${al && al.unread ? `<div class="banner ${unreadReplies ? "ok" : "warn"}">&#128276; <b>${al.unread} new alert(s)</b>${unreadReplies ? `, including <b>${unreadReplies} repl${unreadReplies === 1 ? "y" : "ies"}</b>` : ""}.
      <a href="#followups">Open Follow-ups →</a></div>` : ""}
    ${al && !al.ready ? `<div class="banner warn">Reply alerts are off: ${esc(al.problem)} <a href="#followups">Turn them on in Follow-ups →</a></div>` : ""}
    <div class="cards">
      ${card(s.total_contacts, "Total contacts")}
      ${card(s.awaiting_approval, "Drafts awaiting approval", s.awaiting_approval ? "warn" : "")}
      ${card(s.approved, "Approved drafts", "ok")}
      ${card(s.sent, "Emails sent (live)", "ok")}
      ${card(s.failed_or_uncertain, "Failed or uncertain sends", s.failed_or_uncertain ? "bad" : "")}
      ${card(s.suppressed, "Suppressed contacts")}
    </div>
    <div class="card">
      <div class="meta-row">Sent in the last 24 hours: <b>${s.sent_last_24h}</b> of your daily limit of <b>${s.daily_limit}</b>.</div>
      ${s.active_batch ? `<div class="meta-row">Active batch #${s.active_batch.id}: ${pill(s.active_batch.status)} <a href="#queue">Open sending queue →</a></div>` : ""}
    </div>
    ${d ? `<div class="card">
      <h3>Website discovery</h3>
      <div class="meta-row">${d.stats.total} websites · <b>${d.stats.new_today}</b> new today · <b>${d.stats.qualified}</b> qualified ·
        <b>${d.stats.needs_review}</b> awaiting review · last successful run: <b>${d.stats.last_successful_run ? fmtDate(d.stats.last_successful_run) : "never"}</b>
        ${d.running ? ` · ${pill("sending", "running now")}` : ""}</div>
      <a href="#discovery">Open Website discovery →</a>
    </div>` : ""}
    <div class="card">
      <h3>How it works</h3>
      <ol class="steps">
        <li><a href="#discovery">Discover websites</a> for free (generated searches + URL import), review them, and add qualified ones to your contacts, or
          <a href="#import">import contacts</a> from a CSV or Excel file.</li>
        <li><a href="#contacts">Check contacts</a>: fix flagged issues and mark addresses you found on a legitimate public source as <i>verified</i>.</li>
        <li><a href="#topics">Pick a guest post topic</a> for each website, or write your own.</li>
        <li><a href="#generate">Generate drafts</a> (up to ${MAX_BATCH} at a time). This never sends anything.</li>
        <li><a href="#review">Review, edit and approve</a> each email.</li>
        <li><a href="#queue">Prepare a batch</a>, check the confirmation screen, and type SEND to start.</li>
      </ol>
    </div>`;
};

// ------------------------------------------------------------------ website discovery

const DISCOVERY_PILL = {
  Qualified: "ok", "Needs Review": "warn", Rejected: "bad", New: "",
  Verified: "ok", "Syntax Valid": "blue", "Catch-All": "warn", Invalid: "bad", Unknown: "",
  Yes: "ok", No: "bad",
  "Draft Generated": "blue", "Awaiting Approval": "warn", Approved: "ok", Sent: "ok", Replied: "ok", Bounced: "bad", "Do Not Contact": "bad",
};
function dpill(label) { return `<span class="pill ${DISCOVERY_PILL[label] ?? ""}">${esc(label)}</span>`; }
const DISCOVERY_NICHES = ["Education", "Technology", "Biometric Devices", "Business Services"];
const OUTREACH_LABELS = ["New", "Draft Generated", "Awaiting Approval", "Approved", "Sent", "Replied", "Rejected", "Bounced", "Do Not Contact"];
const metric = (v, checkedAt) => (v === null || v === undefined
  ? `<span class="muted small">${checkedAt ? "No value" : "Not Checked"}</span>`
  : esc(Number.isInteger(v) ? v : v.toFixed(1)));
DISCOVERY_PILL["Not Checked"] = "";
const NICHE_KEYS = [["", "Detect automatically"], ["education", "Education"], ["technology", "Technology"],
  ["biometrics", "Biometric Devices"], ["business_services", "Business Services"]];
// Runs in YOUR browser on a page you opened (e.g. search results): copies the page's outbound links.
const COLLECT_BOOKMARKLET = "javascript:(()=>{const s=new Set();document.querySelectorAll('a[href]').forEach(a=>{try{const u=new URL(a.href,location.href);if(/^https?:$/.test(u.protocol)&&u.hostname!==location.hostname)s.add(u.href)}catch(e){}});const t=[...s].join('\\n');const f=()=>prompt('Copy these '+s.size+' links, then paste them into Website discovery:',t);navigator.clipboard?navigator.clipboard.writeText(t).then(()=>alert(s.size+' links copied. Paste them into Website discovery.'),f):f()})()";

function claudeCard(cs) {
  if (!cs) return "";
  const r = cs.running, last = (cs.runs || []).find((x) => !["running", "inspecting"].includes(x.status));
  const niches = NICHE_KEYS.filter(([k]) => k);
  const runLine = (x) => `<li>#${x.id} ${fmtDate(x.started_at)} (${esc(x.trigger)}): ${pill(x.status === "completed" ? "approved" : x.status === "failed" ? "failed" : "", x.status)}
    ${esc(x.message || "")}${x.usage && x.usage.duration_seconds ? ` <span class="muted">· ${Math.round(x.usage.duration_seconds / 60)} min</span>` : ""}</li>`;
  return `
    <div class="card">
      <div class="page-head" style="margin:0">
        <div><h3 style="margin:0">Claude auto-discovery</h3>
          <p class="small muted" style="margin:4px 0 0">Claude searches the web for new guest-post sites in your niches, skips sites you already have,
          and looks up their DR for free. The app then inspects each site and adds it to the table below. Claude can only search and look up DR:
          it can't send email or change anything else. It uses your Claude plan's usage, with no extra cost.</p></div>
        ${cs.available ? `<button class="btn primary" id="claudeGo" ${r ? "disabled" : ""}>${r ? "Claude is working…" : "Find new websites with Claude"}</button>` : ""}
      </div>
      ${!cs.available ? `<div class="banner warn">Claude Code (the <code>claude</code> command) was not found on this computer, so this option is unavailable.</div>` : `
      ${r ? `<div class="banner info">Run #${r.id} ${r.status === "inspecting" ? `is inspecting ${r.queued} new site(s)…` : "is searching the web…"} Started ${fmtDate(r.started_at)}.
        This usually takes 5 to 15 minutes. You can leave this page; this page refreshes itself.</div>` : ""}
      <div class="toolbar small" style="margin-top:8px">
        <span>Niches:</span>
        ${niches.map(([k, l]) => `<label><input type="checkbox" class="claude-niche" value="${k}" checked> ${esc(l)}</label>`).join(" ")}
        <span class="spacer"></span>
        <label><input type="checkbox" id="claudeDaily" ${cs.daily ? "checked" : ""}> Run automatically once a day when the app starts</label>
      </div>
      ${last ? `<p class="small">Last run: ${esc(last.message || last.status)}</p>` : ""}
      ${(cs.runs || []).length ? `<details><summary class="small">Recent Claude runs (${cs.runs.length})</summary><ul class="small">${cs.runs.map(runLine).join("")}</ul></details>` : ""}`}
    </div>`;
}

function wireClaude(cs) {
  const go = $("#claudeGo");
  if (go) go.onclick = () => run(async () => {
    const nicheList = $$(".claude-niche:checked").map((c) => c.value);
    if (!nicheList.length) throw new Error("Pick at least one niche.");
    const r = await api("/api/claude/discover", { method: "POST", body: { niches: nicheList } });
    toast(r.message, !r.started);
    setTimeout(route, 800);
  });
  const daily = $("#claudeDaily");
  if (daily) daily.onchange = () => run(async () => {
    await api("/api/claude/settings", { method: "PUT", body: { daily: daily.checked } });
    toast(daily.checked ? "Claude will look for new websites once a day when the app starts." : "Daily Claude discovery is off.");
  });
}

function freeDiscoveryCard(cfg, imports) {
  const m = cfg.method || {};
  const nicheOpts = NICHE_KEYS.map(([k, l]) => `<option value="${k}">${esc(l)}</option>`).join("");
  return `
    <div class="card">
      <h3>Discovery method</h3>
      <div class="meta-row">${m.automatic
        ? `${pill(m.paid ? "pending" : "approved", m.paid ? "automatic search (paid service you configured)" : "automatic search (free)")} <b>${esc(m.active)}</b>`
        : `${pill("approved", "free")} <b>Manual search + URL import</b> · no paid API, no API key needed`}</div>
      <p class="small">${esc(m.reason || "")}</p>
      <ul class="config-list small">
        <li><b>DA / DR:</b> ${cfg.moz_configured || cfg.ahrefs_configured ? "looked up only from the provider you configured." : "<b>Not Checked</b>. No reliable free source is configured, and values are never estimated."}
          You can check a site yourself with a free tool (link in each website's Details).</li>
        <li><b>Email status:</b> ${cfg.email_verify_problem ? "<b>Not Checked</b>. No verification service is configured, so addresses are never marked verified." : "checked by " + esc(cfg.email_verify_provider) + "."}
          Sending still needs your own verification, approval and typed confirmation.</li>
      </ul>
    </div>
    <div class="card" id="freeFlow">
      <h3>Free discovery workflow</h3>
      <p class="small muted">The app never queries search engines itself. You run the searches in your own browser, like any visitor,
        and the app inspects the websites you import. It respects robots.txt and request limits, and never bypasses CAPTCHAs or logins.</p>
      <h4>1 · Generate searches</h4>
      <div class="filters">
        <div class="field"><label>Niche</label><select id="fqNiche">${NICHE_KEYS.map(([k, l]) => `<option value="${k}">${esc(k ? l : "All niches")}</option>`).join("")}</select></div>
        <div class="field" style="flex:2"><label>Your own keywords (optional, comma-separated)</label><input type="text" id="fqKw" placeholder="e.g. smart classroom, LMS, attendance system"></div>
        <div class="field"><label>&nbsp;</label><button class="btn" id="fqGen">Generate searches</button></div>
      </div>
      <div id="fqList"></div>
      <h4>2 · Import the result URLs</h4>
      <p class="small">Use any of these:
        <b>(a)</b> select the search results, copy, and paste them below (URLs are picked out of the text);
        <b>(b)</b> press <kbd>Ctrl</kbd>+<kbd>S</kbd> on a results page or a "blogs that accept guest posts" list, then upload the saved <code>.html</code>;
        <b>(c)</b> drag this button to your bookmarks bar, click it on a results page, and paste:
        <a class="btn sm" href="${esc(COLLECT_BOOKMARKLET)}" onclick="event.preventDefault();toast('Drag this button to your bookmarks bar.')">Collect links</a></p>
      <div class="grid2">
        <div class="field"><label>Niche of these results</label><select id="fiNiche">${nicheOpts}</select></div>
        <div class="field"><label>Search they came from (optional)</label><input type="text" id="fiQuery" placeholder='e.g. edtech "write for us"'></div>
      </div>
      <div class="field"><label>Paste URLs, domains or copied search results</label>
        <textarea id="fiText" rows="6" placeholder="https://example-blog.com/write-for-us&#10;another-site.org&#10;…"></textarea></div>
      <div class="toolbar">
        <input type="file" id="fiFile" accept=".txt,.csv,.tsv,.md,.html,.htm">
        <label class="small"><input type="checkbox" id="fiProcess" checked> Start inspecting right away</label>
        <span class="spacer"></span>
        <button class="btn primary" id="fiGo">Import &amp; inspect</button>
      </div>
      <div id="fiResult"></div>
      <h4>3 · Inspection queue</h4>
      <div class="meta-row small">Pending: <b>${imports.pending}</b> · inspected: <b>${imports.counts?.processed ?? 0}</b> ·
        already known: <b>${imports.counts?.already_known ?? 0}</b> · skipped: <b>${imports.counts?.skipped ?? 0}</b> ·
        errors: <b>${imports.counts?.error ?? 0}</b> · up to ${cfg.import_max_per_run} imported websites are inspected per run.
        ${imports.pending ? `<button class="btn sm" id="fiRun">Process pending imports now</button>` : ""}</div>
      <details id="fiRecentBox"><summary class="small">Recent imports</summary><div id="fiRecent" class="small muted">Loading…</div></details>
    </div>`;
}

function wireFreeDiscovery() {
  const renderQueries = (items) => {
    $("#fqList").innerHTML = items.length ? `<div class="table-wrap"><table><thead><tr><th>Search</th><th>Open in your browser</th><th></th></tr></thead><tbody>
      ${items.map((q, i) => `<tr><td class="wrap"><code>${esc(q.query)}</code>${q.niche_label ? ` <span class="small muted">${esc(q.niche_label)}</span>` : ""}</td>
        <td>${Object.entries(q.links).map(([n, u]) => `<a class="btn sm" href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(n)}</a>`).join(" ")}</td>
        <td><button class="btn sm" data-qcopy="${i}">Copy</button> <button class="btn sm" data-quse="${i}">Import results for this</button></td></tr>`).join("")}
      </tbody></table></div>` : `<p class="small muted">No searches generated.</p>`;
    $$("[data-qcopy]").forEach((b) => (b.onclick = () => run(async () => { await navigator.clipboard.writeText(items[b.dataset.qcopy].query); toast("Search copied."); })));
    $$("[data-quse]").forEach((b) => (b.onclick = () => {
      const q = items[b.dataset.quse];
      $("#fiQuery").value = q.query;
      if (q.niche) $("#fiNiche").value = q.niche;
      $("#fiText").focus();
    }));
  };
  $("#fqGen").onclick = () => run(async () => {
    const p = new URLSearchParams({ niche: $("#fqNiche").value, keywords: $("#fqKw").value });
    renderQueries(await api("/api/discovery/queries?" + p));
  });
  $("#fiGo").onclick = () => run(async () => {
    const file = $("#fiFile").files[0], process = $("#fiProcess").checked;
    let r;
    if (file) {
      const fd = new FormData();
      fd.append("file", file); fd.append("niche", $("#fiNiche").value); fd.append("query", $("#fiQuery").value);
      fd.append("process", process ? "true" : "false");
      r = await api("/api/discovery/import-file", { method: "POST", body: fd });
    } else {
      r = await api("/api/discovery/import", { method: "POST", body: { text: $("#fiText").value, niche: $("#fiNiche").value, query: $("#fiQuery").value, process } });
    }
    const list = (arr, f) => arr.length ? `<ul class="small">${arr.slice(0, 30).map((x) => `<li>${f(x)}</li>`).join("")}${arr.length > 30 ? `<li>… and ${arr.length - 30} more</li>` : ""}</ul>` : "";
    $("#fiResult").innerHTML = `<div class="banner ${r.queued.length ? "ok" : "warn"}">
      Found <b>${r.found_links}</b> link(s) covering <b>${r.websites_in_input}</b> website(s), after removing <b>${r.duplicates_in_input}</b> duplicate(s) by root domain.
      Queued <b>${r.queued.length}</b> new website(s) for inspection.
      ${r.already_known.length ? `<br><b>${r.already_known.length}</b> already in your database (not added again):` + list(r.already_known, (x) => `${esc(x.domain)} (${esc(x.discovery_status)})`) : ""}
      ${r.already_queued.length ? `<br><b>${r.already_queued.length}</b> already waiting in the queue.` : ""}
      ${r.skipped.length ? `<details><summary class="small">${r.skipped.length} skipped (search engines, platforms, the page itself)</summary>${list(r.skipped, (x) => `${esc(x.domain)}: ${esc(x.reason)}`)}</details>` : ""}
      ${r.invalid ? `<br>${r.invalid} item(s) weren't valid website URLs.` : ""}
      <br>${esc(r.run.message)}${r.run.started ? " Results appear in the table below as each website is inspected." : ""}</div>`;
    $("#fiText").value = ""; $("#fiFile").value = "";
    if (r.run.started) setTimeout(route, 1500);
  });
  const runBtn = $("#fiRun");
  if (runBtn) runBtn.onclick = () => run(async () => {
    const r = await api("/api/discovery/run", { method: "POST" });
    toast(r.message, !r.started);
    setTimeout(route, 800);
  });
  $("#fiRecentBox").addEventListener("toggle", () => run(async () => {
    const q = await api("/api/discovery/import-queue");
    $("#fiRecent").innerHTML = q.recent.length ? `<table><thead><tr><th>Website</th><th>Status</th><th>Result</th><th>Imported</th></tr></thead><tbody>
      ${q.recent.map((x) => `<tr><td class="wrap"><a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.domain)}</a></td>
        <td>${pill(x.status === "processed" ? "approved" : x.status === "pending" ? "pending" : x.status === "error" ? "failed" : "", x.status.replace("_", " "))}</td>
        <td class="wrap">${x.discovery_status ? dpill(x.discovery_status) : ""} ${esc(x.note)}${x.email ? `<br>${esc(x.email)}` : ""}</td>
        <td>${fmtDate(x.created_at)}</td></tr>`).join("")}</tbody></table>` : "No imports yet.";
  }), { once: true });
}

function discoveryQuery() {
  const f = pages.discovery.filters || {};
  const p = new URLSearchParams();
  Object.entries(f).forEach(([k, v]) => { if (v !== "" && v != null) p.set(k, v); });
  return p;
}

pages.discovery = async () => {
  pages.discovery.filters = pages.discovery.filters || { sort: "discovered", direction: "desc" };
  const f = pages.discovery.filters;
  const [st, list, cs] = await Promise.all([api("/api/discovery/status"), api("/api/discovery/websites?" + discoveryQuery()),
    api("/api/claude/status").catch(() => null)]);
  const s = st.stats, cfg = st.config, last = st.last_completed, running = st.running;
  const card = (num, label, cls = "") => `<div class="card stat ${cls}"><div class="num">${num}</div><div class="label">${label}</div></div>`;
  const cfgLine = (ok, label, problem) => `<li>${ok ? pill("approved", "ready") : pill("pending", "not set")} ${esc(label)}${problem ? ` <span class="small muted">— ${esc(problem)}</span>` : ""}</li>`;
  const opt = (vals, cur, all = "Any") => `<option value="">${all}</option>` + vals.map((v) => `<option ${v === cur ? "selected" : ""}>${esc(v)}</option>`).join("");
  const lastRun = (r) => {
    if (!r) return `<p class="muted small">No discovery run has completed yet.</p>`;
    const x = r.summary || {}, u = r.api_usage || {};
    return `<p class="small">Run #${r.id} (${esc(r.trigger)}) finished ${fmtDate(r.finished_at)}:
      <b>${x.new_websites ?? 0}</b> new, <b>${x.existing_websites ?? 0}</b> already known, <b>${x.duplicate_results ?? 0}</b> duplicate results,
      <b>${x.skipped_platforms ?? 0}</b> platforms/lists skipped · <b>${x.qualified ?? 0}</b> qualified, <b>${x.needs_review ?? 0}</b> need review,
      <b>${x.rejected ?? 0}</b> rejected.<br>
      ${x.method ? `Method: <b>${esc(x.method === "manual" ? "free manual import" : x.method)}</b> · imported websites inspected: <b>${x.imported_processed ?? 0}</b>, still pending: <b>${x.imports_pending ?? 0}</b><br>` : ""}
      Usage: ${u.searches ?? 0} automatic searches (${u.cached_queries ?? 0} cached queries reused), ${u.page_requests ?? 0} page requests,
      ${u.moz_lookups ?? 0} Moz lookups, ${u.ahrefs_requests ?? 0} Ahrefs requests, ${u.email_verifications ?? 0} email checks.</p>
      ${(x.notices || []).length ? `<details><summary class="small">${x.notices.length} note(s)</summary><ul class="small">${x.notices.map((e) => `<li>${esc(e)}</li>`).join("")}</ul></details>` : ""}
      ${(r.errors || []).length ? `<details><summary class="small">${r.errors.length} warning(s)/error(s)</summary><ul class="small">${r.errors.map((e) => `<li>${esc(String(e).split("\n")[0])}</li>`).join("")}</ul></details>` : ""}`;
  };

  main.innerHTML = `
    <div class="page-head">
      <div><h1>Website discovery</h1><p class="lead">Finds websites in your niches that accept contributions. Discovery only collects
      information: it never creates drafts or sends email.</p></div>
      <button class="btn primary" id="runNow" ${running ? "disabled" : ""} title="${cfg.method && !cfg.method.automatic ? "Free mode: inspects the imported websites waiting in the queue. It does not search the internet by itself." : ""}">${running ? "Discovery running…" : cfg.method && !cfg.method.automatic ? `Run Discovery (${(st.imports || {}).pending ?? 0} waiting)` : "Run Website Discovery Now"}</button>
    </div>
    ${running ? `<div class="banner info">Run #${running.id} started ${fmtDate(running.started_at)}. New: <b>${running.summary.new_websites ?? 0}</b>,
      qualified: <b>${running.summary.qualified ?? 0}</b> so far. This page refreshes automatically.</div>` : ""}
    <div class="cards">
      ${card(s.total, "Websites discovered")}
      ${card(s.new_today, "New today")}
      ${card(s.qualified, "Qualified", "ok")}
      ${card(s.needs_review, "Awaiting review", s.needs_review ? "warn" : "")}
      ${card(s.missing_email, "Missing contact email")}
      ${card(s.verified_email, "Verified emails", "ok")}
      ${card(`${s.missing_da} / ${s.missing_dr}`, "DA / DR not checked")}
      ${card(s.duplicates_skipped_last_run, "Duplicates skipped (last run)")}
      ${card(s.drafts_awaiting_approval, "Drafts awaiting approval", s.drafts_awaiting_approval ? "warn" : "")}
    </div>
    ${claudeCard(cs)}
    ${freeDiscoveryCard(cfg, st.imports || { counts: {}, pending: 0 })}
    <div class="card">
      <div class="meta-row">Last successful run: <b>${s.last_successful_run ? fmtDate(s.last_successful_run) : "never"}</b>
        · ${st.completed_today ? "Already ran today" : "Has not completed today"}${cfg.method && cfg.method.automatic ? ` · Searches this month: <b>${st.searches_this_month}</b> of ${cfg.max_searches_month}` : " · Automatic searches: off (free mode)"}</div>
      ${lastRun(last)}
      <details><summary class="small">Services and limits</summary>
        <ul class="config-list">
          <li>${pill("approved", "ready")} Free discovery: generated searches + URL import (always available, no key needed)</li>
          ${cfgLine(cfg.method && cfg.method.automatic, `Optional automatic search: ${cfg.method ? cfg.method.configured : cfg.search_provider}`,
            cfg.method && !cfg.method.automatic ? "off. Free option: your own SearXNG (SEARCH_PROVIDER=searxng + SEARXNG_URL)." : "")}
          ${cfgLine(cfg.moz_configured, "Moz Domain Authority (DA)", cfg.moz_configured ? "" : "optional; DA stays Not Checked")}
          ${cfgLine(cfg.ahrefs_configured, `Ahrefs Domain Rating (DR), ${cfg.ahrefs_endpoint} endpoint`, cfg.ahrefs_configured ? "" : "optional; DR stays Not Checked")}
          ${cfgLine(!cfg.email_verify_problem, `Email verification: ${cfg.email_verify_provider}`, cfg.email_verify_problem ? "optional; email status stays Not Checked" : "")}
          <li class="small">Per run: up to ${cfg.daily_limit} new websites, ${cfg.max_searches} searches, ${cfg.max_requests_per_domain} requests per website.
            Automatic run on app start: ${cfg.auto_on_startup ? "on" : "off"}. Change these in <code>.env</code>.</li>
          <li class="small">CSV file: <code>${esc(cfg.csv_path)}</code></li>
        </ul>
      </details>
    </div>

    <div class="card">
      <div class="filters">
        <div class="field"><label>Niche</label><select data-f="niche">${opt(DISCOVERY_NICHES, f.niche)}</select></div>
        <div class="field"><label>Discovery status</label><select data-f="discovery_status">${opt(["Qualified", "Needs Review", "Rejected", "New"], f.discovery_status)}</select></div>
        <div class="field"><label>Outreach status</label><select data-f="outreach_status">${opt(OUTREACH_LABELS, f.outreach_status)}</select></div>
        <div class="field"><label>Email check</label><select data-f="email_verification">${opt(s.verify_statuses, f.email_verification)}</select></div>
        <div class="field"><label>Accepts guest posts</label><select data-f="accepts">${opt(["Yes", "No", "Unknown"], f.accepts)}</select></div>
        <div class="field"><label>Missing</label><select data-f="missing">${opt(["email", "da", "dr"], f.missing, "—")}</select></div>
        <div class="field"><label>DA from</label><input type="number" min="0" max="100" data-f="da_min" value="${esc(f.da_min ?? "")}"></div>
        <div class="field"><label>DA to</label><input type="number" min="0" max="100" data-f="da_max" value="${esc(f.da_max ?? "")}"></div>
        <div class="field"><label>DR from</label><input type="number" min="0" max="100" data-f="dr_min" value="${esc(f.dr_min ?? "")}"></div>
        <div class="field"><label>DR to</label><input type="number" min="0" max="100" data-f="dr_max" value="${esc(f.dr_max ?? "")}"></div>
        <div class="field"><label>Discovered from</label><input type="date" data-f="date_from" value="${esc(f.date_from ?? "")}"></div>
        <div class="field"><label>Discovered to</label><input type="date" data-f="date_to" value="${esc(f.date_to ?? "")}"></div>
        <div class="field"><label>Sort by</label><select data-f="sort">
          ${[["discovered", "Discovery date"], ["da", "DA"], ["dr", "DR"], ["niche", "Niche"], ["confidence", "Confidence"], ["name", "Name"]]
            .map(([v, l]) => `<option value="${v}" ${f.sort === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div class="field"><label>Order</label><select data-f="direction">
          <option value="desc" ${f.direction !== "asc" ? "selected" : ""}>High → low / newest</option>
          <option value="asc" ${f.direction === "asc" ? "selected" : ""}>Low → high / oldest</option></select></div>
        <div class="field"><label>Search</label><input type="text" data-f="q" placeholder="domain, name, email" value="${esc(f.q ?? "")}"></div>
      </div>
      <div class="toolbar" style="margin-top:10px">
        <button class="btn" id="applyF">Apply filters</button>
        <button class="btn" id="clearF">Clear</button>
        <span class="spacer"></span>
        <a class="btn" id="expFiltered" href="/api/discovery/export.csv?scope=filtered&${discoveryQuery()}">Export filtered (${list.length})</a>
        <a class="btn" href="/api/discovery/export.csv?scope=all">Export all</a>
        <button class="btn" id="writeCsv">Update CSV file</button>
      </div>
    </div>

    <div class="toolbar">
      <button class="btn primary" id="toContacts">Add selected to outreach contacts</button>
      <button class="btn ok" id="markQ">Mark Qualified</button>
      <button class="btn" id="markR">Mark Needs Review</button>
      <button class="btn danger" id="markX">Reject</button>
      <span class="small muted">${list.length} website(s) shown. DA is Moz Domain Authority; DR is
        <a href="https://ahrefs.com/" target="_blank" rel="noopener noreferrer">Domain Rating by Ahrefs</a>. They're separate metrics from different providers, and neither one shows on its own that a site is high quality.</span>
    </div>
    <div id="disResult"></div>
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" class="check-all" aria-label="Select all"></th><th>Website</th><th>Niche</th><th>Contact</th>
        <th>DA</th><th>DR</th><th>Guest posts</th><th>Discovery</th><th>Outreach</th><th>Discovered</th><th></th></tr></thead>
      <tbody>${list.length ? list.map((w) => `<tr>
        <td><input type="checkbox" class="row-check" value="${w.id}"></td>
        <td class="wrap"><b>${esc(w.website_name) || esc(w.domain)}</b><br>
          <a class="small" href="${esc(w.website_url)}" target="_blank" rel="noopener noreferrer">${esc(w.domain)}</a>
          ${w.guidelines_url ? `<br><a class="small" href="${esc(w.guidelines_url)}" target="_blank" rel="noopener noreferrer">Guidelines</a>` : ""}
          ${w.article_url ? ` · <a class="small" href="${esc(w.article_url)}" target="_blank" rel="noopener noreferrer">Article</a>` : ""}</td>
        <td>${esc(w.niche) || "<span class='muted'>—</span>"}</td>
        <td class="wrap">${w.email ? `${esc(w.email)}<br>${dpill(w.email_verification)}` : "<span class='muted small'>No public email found</span>"}
          ${w.contact_name ? `<br><span class="small">${esc(w.contact_name)}</span>` : ""}</td>
        <td class="metric" title="${esc(w.da_source || "Not Checked")}">${metric(w.da, w.da_checked_at)}</td>
        <td class="metric" title="${esc(w.dr_source || "Not Checked")}">${metric(w.dr, w.dr_checked_at)}</td>
        <td>${dpill(w.accepts_guest_posts)}</td>
        <td>${dpill(w.discovery_status)}<div class="small muted">${w.confidence}% confidence</div>
          ${w.review_reason ? `<div class="small" style="color:var(--warn)">${esc(w.review_reason)}</div>` : ""}</td>
        <td>${dpill(w.outreach_label)}${w.contact_id ? `<br><a class="small" href="#contacts">in contacts</a>` : ""}</td>
        <td class="small">${esc(w.discovered_date)}</td>
        <td><button class="btn sm" data-wdetail="${w.id}">Details</button></td>
      </tr>`).join("") : emptyRow(11, "No websites match. Import URLs above, or clear the filters.")}</tbody></table></div>`;

  wireSelectAll();
  wireFreeDiscovery();
  wireClaude(cs);
  const readFilters = () => {
    const nf = {};
    $$("[data-f]").forEach((el) => { if (el.value !== "") nf[el.dataset.f] = el.value; });
    pages.discovery.filters = { sort: "discovered", direction: "desc", ...nf };
  };
  $("#applyF").onclick = () => { readFilters(); route(); };
  $$(".filters select").forEach((el) => (el.onchange = () => { readFilters(); route(); }));
  $("#clearF").onclick = () => { pages.discovery.filters = null; route(); };
  $("#runNow").onclick = () => run(async () => {
    const r = await api("/api/discovery/run", { method: "POST" });
    if (r.nothing_to_do) {
      $("#fiResult").innerHTML = `<div class="banner warn"><b>Nothing to inspect yet.</b> ${esc(r.message)}
        Paste website URLs or copied search results in the box above and click <b>Import &amp; inspect</b>.</div>`;
      $("#fiText").scrollIntoView({ behavior: "smooth", block: "center" });
      $("#fiText").focus();
      return;
    }
    toast(r.message, !r.started);
    setTimeout(route, 800);
  });
  $("#writeCsv").onclick = () => run(async () => {
    const r = await api("/api/discovery/export-file", { method: "POST" });
    toast(r.warning || `Saved ${r.rows} row(s) to ${r.path}`, !!r.warning);
  });
  const setStatus = (status) => run(async () => {
    const ids = selectedIds();
    if (!ids.length) throw new Error("Select websites first.");
    await api("/api/discovery/websites/status", { method: "POST", body: { ids, status } });
    toast(`Marked ${status}.`);
    route();
  });
  $("#markQ").onclick = () => setStatus("Qualified");
  $("#markR").onclick = () => setStatus("Needs Review");
  $("#markX").onclick = () => setStatus("Rejected");
  $("#toContacts").onclick = () => run(async () => {
    const ids = selectedIds();
    if (!ids.length) throw new Error("Select websites first.");
    const r = await api("/api/discovery/websites/to-contacts", { method: "POST", body: { ids } });
    $("#disResult").innerHTML = `<div class="banner ${r.created.length || r.linked.length ? "ok" : "warn"}">
      Added <b>${r.created.length}</b> new contact(s), linked <b>${r.linked.length}</b> existing. New contacts start as
      <b>unverified</b>: open <a href="#contacts">Manage contacts</a>, check each address's source, and mark it verified before it can be sent to.
      ${r.skipped.length ? `<ul class="small">${r.skipped.map((x) => `<li>${esc(x.domain || "#" + x.website_id)}: ${esc(x.reason)}</li>`).join("")}</ul>` : ""}</div>`;
  });
  $$("[data-wdetail]").forEach((b) => (b.onclick = () => websiteDetail(list.find((w) => w.id === Number(b.dataset.wdetail)))));
  if (running || (cs && cs.running)) pollTimer = setInterval(() => { if (location.hash === "#discovery") route(); }, running ? 5000 : 15000);
};

async function websiteDetail(w) {
  const changes = await api(`/api/discovery/websites/${w.id}/changes`).catch(() => []);
  const v = (k) => esc(w[k] ?? "");
  const linked = !!w.contact_id;
  openModal(w.website_name || w.domain, `
    <div class="meta-row"><b>Domain:</b> ${esc(w.domain)} · <b>Found by:</b> ${esc(w.discovery_query) || "—"}
      ${w.source_url ? ` · <a href="${esc(w.source_url)}" target="_blank" rel="noopener noreferrer">source URL</a>` : ""}</div>
    <div class="meta-row"><b>DA:</b> ${metric(w.da, w.da_checked_at)} <span class="small muted">${esc(w.da_source)} ${w.da_checked_at ? "· " + fmtDate(w.da_checked_at) : ""}</span></div>
    <div class="meta-row"><b>DR:</b> ${metric(w.dr, w.dr_checked_at)} <span class="small muted">${esc(w.dr_source)} ${w.dr_checked_at ? "· " + fmtDate(w.dr_checked_at) : ""}</span></div>
    ${w.da === null || w.dr === null ? `<div class="meta-row small muted">Check it yourself for free (values are not copied into the app):
      <a href="https://ahrefs.com/website-authority-checker?input=${encodeURIComponent(w.domain)}" target="_blank" rel="noopener noreferrer">Ahrefs Website Authority Checker</a> ·
      <a href="https://moz.com/domain-analysis?site=${encodeURIComponent(w.domain)}" target="_blank" rel="noopener noreferrer">Moz Domain Analysis</a></div>` : ""}
    <div class="meta-row"><b>Email check:</b> ${dpill(w.email_verification)} <span class="small muted">${esc(w.email_verification_source)}</span>
      ${w.contact_source_url ? ` · <b>Source:</b> ${/^https?:/.test(w.contact_source_url) ? `<a href="${esc(w.contact_source_url)}" target="_blank" rel="noopener noreferrer">${esc(w.contact_source_url)}</a>` : esc(w.contact_source_url)}` : ""}</div>
    ${w.article_url ? `<div class="meta-row"><b>Article:</b> ${esc(w.article_title) || esc(w.article_url)} <span class="small muted">(${w.article_retrieved ? "page retrieved" : "link only, not opened"})</span></div>` : ""}
    <div class="meta-row"><b>Evidence (${w.confidence}% confidence):</b></div>
    <ul class="evidence">${(w.evidence || []).map((e) => `<li>${esc(e)}</li>`).join("") || "<li>None recorded</li>"}</ul>
    <hr>
    <div class="grid2">
      <div class="field"><label>Website name</label><input type="text" id="w_website_name" value="${v("website_name")}"></div>
      <div class="field"><label>Niche</label><select id="w_niche">${["", ...DISCOVERY_NICHES].map((n) => `<option ${n === w.niche ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></div>
      <div class="field"><label>Contact name</label><input type="text" id="w_contact_name" value="${v("contact_name")}"></div>
      <div class="field"><label>Email ${linked ? "(edit in Manage contacts)" : ""}</label><input type="email" id="w_email" value="${v("email")}" ${linked ? "disabled" : ""}></div>
      <div class="field"><label>Guidelines URL</label><input type="url" id="w_guidelines_url" value="${v("guidelines_url")}"></div>
      <div class="field"><label>Relevant article URL</label><input type="url" id="w_article_url" value="${v("article_url")}"></div>
      <div class="field"><label>Accepts guest posts</label><select id="w_accepts_guest_posts">${["Yes", "No", "Unknown"].map((n) => `<option ${n === w.accepts_guest_posts ? "selected" : ""}>${n}</option>`).join("")}</select></div>
      <div class="field"><label>Discovery status</label><select id="w_discovery_status">${["Qualified", "Needs Review", "Rejected", "New"].map((n) => `<option ${n === w.discovery_status ? "selected" : ""}>${n}</option>`).join("")}</select></div>
    </div>
    <div class="field"><label>Suggested topic</label><input type="text" id="w_suggested_topic" value="${v("suggested_topic")}"></div>
    <div class="field"><label>Personalization notes</label><textarea id="w_personalization_notes">${v("personalization_notes")}</textarea></div>
    ${!linked ? `<div class="field"><label>Outreach status</label><select id="w_outreach_status">${["New", "Rejected", "Do Not Contact"].map((n) => `<option ${n === w.outreach_label ? "selected" : ""}>${n}</option>`).join("")}</select>
      <div class="hint">"Do Not Contact" also adds the email to your suppression list.</div></div>` : `<p class="small muted">Outreach status (${esc(w.outreach_label)}) comes from this website's contact, drafts and sending history.</p>`}
    <div class="toolbar"><button class="btn primary" id="saveWebsite">Save</button><button class="btn" onclick="closeModal()">Cancel</button></div>
    ${changes.length ? `<details><summary class="small">Change history (${changes.length})</summary><ul class="small">${changes.map((c) =>
      `<li>${fmtDate(c.created_at)}: ${esc(c.field)} → ${esc(String(c.new_value).slice(0, 80))} <span class="muted">(${esc(c.source)})</span></li>`).join("")}</ul></details>` : ""}`);
  $("#saveWebsite").onclick = () => run(async () => {
    const body = {};
    ["website_name", "niche", "contact_name", "guidelines_url", "article_url", "accepts_guest_posts", "discovery_status", "suggested_topic", "personalization_notes"]
      .forEach((k) => { if (String($(`#w_${k}`).value) !== String(w[k] ?? "")) body[k] = $(`#w_${k}`).value; });
    if (!linked && $("#w_email").value !== (w.email || "")) body.email = $("#w_email").value;
    const os = $("#w_outreach_status");
    if (os && os.value !== w.outreach_label) {
      if (os.value === "Do Not Contact" && !confirm("Add this website's email to the do-not-contact list?")) return;
      body.outreach_status = os.value;
    }
    if (!Object.keys(body).length) return closeModal();
    await api(`/api/discovery/websites/${w.id}`, { method: "PUT", body });
    closeModal();
    toast("Saved.");
    route();
  });
}

// ------------------------------------------------------------------ 2. import

pages.import = async () => {
  main.innerHTML = `
    <h1>Import contacts</h1>
    <p class="lead">Upload a .csv or .xlsx file. Column names are matched flexibly (e.g. "Email", "Contact Email").</p>
    <div class="card">
      <div class="field">
        <label for="file">Contact file</label>
        <input type="file" id="file" accept=".csv,.xlsx,.xlsm">
      </div>
      <div class="toolbar">
        <button class="btn primary" id="upload">Upload and import</button>
        <a class="btn" href="/api/import/template.csv">Download CSV template</a>
      </div>
      <p class="hint">Supported columns: Website Name, Website URL, Contact Name, Email, Niche, Relevant Article URL, Suggested Topic,
      Personalization Notes, Outreach Status, Email Verified (yes/no). Only use addresses that the website publishes for
      editorial or contribution enquiries.</p>
    </div>
    <div id="report"></div>`;
  $("#upload").onclick = () => run(async () => {
    const f = $("#file").files[0];
    if (!f) throw new Error("Choose a file first.");
    const fd = new FormData();
    fd.append("file", f);
    const r = await api("/api/import", { method: "POST", body: fd });
    const list = (items, fmt) => items.length ? `<ul class="small">${items.map(fmt).join("")}</ul>` : "";
    $("#report").innerHTML = `
      <div class="card">
        <h3>Import report</h3>
        <p>Rows read: <b>${r.total_rows}</b> · Imported: <b>${r.imported}</b> · Duplicates skipped: <b>${r.duplicates.length}</b>
        · Invalid emails (imported, flagged): <b>${r.invalid.length}</b> · Rows without email: <b>${r.missing_email}</b></p>
        ${r.duplicates.length ? `<h4>Duplicates (not imported)</h4>${list(r.duplicates, (d) => `<li>Row ${d.row}: ${esc(d.email)}</li>`)}` : ""}
        ${r.invalid.length ? `<h4>Invalid emails, fix these in Manage contacts</h4>${list(r.invalid, (d) => `<li>Row ${d.row}: ${esc(d.email)} (${esc(d.reason)})</li>`)}` : ""}
        ${r.suppressed.length ? `<h4>On your do-not-contact list</h4>${list(r.suppressed, (d) => `<li>Row ${d.row}: ${esc(d.email)}</li>`)}` : ""}
        <a class="btn primary" href="#contacts">Go to Manage contacts →</a>
      </div>`;
    toast(`Imported ${r.imported} contact(s).`);
  });
};

// ------------------------------------------------------------------ 3. contacts

const STATUSES = ["new", "drafted", "contacted", "replied", "not_interested", "bounced", "opted_out", "do_not_contact"];

pages.contacts = async () => {
  const [contacts, suppression] = await Promise.all([api("/api/contacts"), api("/api/suppression")]);
  const filter = pages.contacts.filter || "all";
  const shown = contacts.filter((c) =>
    filter === "all" ? true :
    filter === "issues" ? c.issues.length > 0 :
    filter === "unverified" ? !c.email_verified : c.outreach_status === filter);
  main.innerHTML = `
    <div class="page-head">
      <div><h1>Manage contacts</h1><p class="lead">${contacts.length} contact(s). Fix flagged issues before generating drafts.</p></div>
      <button class="btn primary" id="addContact">+ Add contact</button>
    </div>
    <div class="toolbar">
      <select id="filter" style="width:auto">
        <option value="all">All contacts</option>
        <option value="issues">With issues</option>
        <option value="unverified">Unverified email source</option>
        ${STATUSES.map((s) => `<option value="${s}">Status: ${s.replace(/_/g, " ")}</option>`).join("")}
      </select>
      <span class="spacer"></span>
      <button class="btn" id="verifySel">Mark selected verified</button>
      <button class="btn" id="unverifySel">Mark unverified</button>
      <button class="btn danger" id="deleteSel">Remove selected</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" class="check-all" aria-label="Select all"></th><th>Website</th><th>Contact</th><th>Email</th>
      <th>Niche</th><th>Status</th><th>Issues</th><th>Actions</th></tr></thead>
      <tbody>${shown.length ? shown.map((c) => `
        <tr>
          <td><input type="checkbox" class="row-check" value="${c.id}"></td>
          <td class="wrap"><b>${esc(c.website_name) || "<i class='muted'>missing</i>"}</b><br>
            ${c.website_url ? `<a class="small" href="${esc(c.website_url)}" target="_blank" rel="noopener noreferrer">${esc(c.website_url)}</a>` : ""}</td>
          <td>${esc(c.contact_name) || "<span class='muted'>—</span>"}</td>
          <td class="wrap">${esc(c.email)} ${c.email_verified ? pill("approved", "verified") : pill("pending", "unverified")}
            ${c.suppressed ? pill("do_not_contact", "suppressed") : ""}</td>
          <td>${esc(c.niche_label || c.niche)}</td>
          <td>${pill(c.outreach_status)}</td>
          <td>${c.issues.length ? `<ul class="issues">${c.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : pill("approved", "OK")}</td>
          <td><div class="actions"><button class="btn sm" data-edit="${c.id}">Edit</button>
            <button class="btn sm danger" data-del="${c.id}">Remove</button></div></td>
        </tr>`).join("") : emptyRow(8, "No contacts here yet. Import a file to get started.")}
      </tbody></table></div>

    <h2>Do-not-contact / suppression list</h2>
    <div class="card">
      <p class="small muted">Addresses here can never receive drafts or emails. Marking a contact as <i>opted out</i> or
      <i>do not contact</i> adds them automatically and cancels their pending drafts and follow-ups.</p>
      <div class="toolbar">
        <input type="email" id="supEmail" placeholder="email@example.com" style="max-width:320px">
        <button class="btn" id="supAdd">Add to list</button>
      </div>
      ${suppression.length ? `<div class="table-wrap"><table><thead><tr><th>Email</th><th>Reason</th><th>Added</th><th></th></tr></thead><tbody>
        ${suppression.map((s) => `<tr><td>${esc(s.email_norm)}</td><td>${esc(s.reason.replace(/_/g, " "))}</td><td>${fmtDate(s.created_at)}</td>
        <td><button class="btn sm" data-unsup="${esc(s.email_norm)}">Remove</button></td></tr>`).join("")}</tbody></table></div>`
        : `<p class="muted small">The list is empty.</p>`}
    </div>`;

  $("#filter").value = filter;
  $("#filter").onchange = (e) => { pages.contacts.filter = e.target.value; route(); };
  wireSelectAll();
  $("#addContact").onclick = () => contactForm(null);
  $$("[data-edit]").forEach((b) => (b.onclick = () => contactForm(contacts.find((c) => c.id === Number(b.dataset.edit)))));
  $$("[data-del]").forEach((b) => (b.onclick = () => removeContacts([Number(b.dataset.del)])));
  $("#deleteSel").onclick = () => removeContacts(selectedIds());
  $("#verifySel").onclick = () => bulkVerify(true);
  $("#unverifySel").onclick = () => bulkVerify(false);
  $("#supAdd").onclick = () => run(async () => {
    const r = await api("/api/suppression", { method: "POST", body: { email: $("#supEmail").value } });
    toast(`Added. ${r.drafts_cancelled} pending draft(s) cancelled.`);
    route();
  });
  $$("[data-unsup]").forEach((b) => (b.onclick = () => run(async () => {
    if (!confirm(`Remove ${b.dataset.unsup} from the do-not-contact list?`)) return;
    await api(`/api/suppression/${encodeURIComponent(b.dataset.unsup)}`, { method: "DELETE" });
    route();
  })));
};

async function bulkVerify(verified) {
  const ids = selectedIds();
  if (!ids.length) return toast("Select contacts first.", true);
  await run(() => api("/api/contacts/verify", { method: "POST", body: { ids, verified } }));
  route();
}

async function removeContacts(ids) {
  if (!ids.length) return toast("Select contacts first.", true);
  if (!confirm(`Remove ${ids.length} contact(s) and all of their unsent drafts? Sent history is kept.`)) return;
  await run(() => api("/api/contacts/delete", { method: "POST", body: { ids } }));
  toast("Removed.");
  route();
}

function contactForm(c) {
  const v = (k) => esc(c ? c[k] ?? "" : "");
  openModal(c ? "Edit contact" : "Add contact", `
    <div class="grid2">
      <div class="field"><label>Website name</label><input type="text" id="f_website_name" value="${v("website_name")}"></div>
      <div class="field"><label>Website URL</label><input type="url" id="f_website_url" value="${v("website_url")}"></div>
      <div class="field"><label>Contact / editor name</label><input type="text" id="f_contact_name" value="${v("contact_name")}"></div>
      <div class="field"><label>Email</label><input type="email" id="f_email" value="${v("email")}"></div>
      <div class="field"><label>Niche</label><input type="text" id="f_niche" value="${v("niche")}" placeholder="e.g. Education, EdTech, Software">
        <div class="hint">Education, EdTech, Technology, Biometric devices, Business services, Business growth, Software</div></div>
      <div class="field"><label>Relevant article or category URL</label><input type="url" id="f_article_url" value="${v("article_url")}"></div>
      <div class="field"><label>Suggested guest post topic</label><input type="text" id="f_suggested_topic" value="${v("suggested_topic")}"></div>
      <div class="field"><label>Outreach status</label><select id="f_outreach_status">
        ${STATUSES.map((s) => `<option value="${s}" ${c && c.outreach_status === s ? "selected" : ""}>${s.replace(/_/g, " ")}</option>`).join("")}</select></div>
    </div>
    <div class="field"><label>Personalization notes (private, not inserted into emails)</label><textarea id="f_notes">${v("notes")}</textarea></div>
    <div class="field"><label class="check"><input type="checkbox" id="f_email_verified" ${c && c.email_verified ? "checked" : ""}>
      I found this address on a legitimate public source (e.g. the site's contact or "write for us" page)</label></div>
    <div class="toolbar"><button class="btn primary" id="saveContact">Save</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
  $("#saveContact").onclick = () => run(async () => {
    const body = {};
    ["website_name", "website_url", "contact_name", "email", "niche", "article_url", "suggested_topic", "notes", "outreach_status"]
      .forEach((k) => (body[k] = $(`#f_${k}`).value));
    body.email_verified = $("#f_email_verified").checked;
    if (c) {
      if (body.outreach_status !== c.outreach_status && ["opted_out", "do_not_contact"].includes(body.outreach_status) &&
          !confirm("This adds the address to the do-not-contact list and cancels pending drafts. Continue?")) return;
      await api(`/api/contacts/${c.id}`, { method: "PUT", body });
    } else {
      await api("/api/contacts", { method: "POST", body });
    }
    closeModal();
    toast("Saved.");
    route();
  });
}

// ------------------------------------------------------------------ 4. topics

pages.topics = async () => {
  const contacts = (await api("/api/contacts")).filter((c) => !["opted_out", "do_not_contact"].includes(c.outreach_status));
  main.innerHTML = `
    <h1>Guest post topics</h1>
    <p class="lead">Three suggestions per website, based on its niche and the category URL you provided. Choose one or write your own.
    Suggestions are ideas only; they have not been checked against the site's existing articles.</p>
    <div class="toolbar">
      <button class="btn primary" id="genTopics">Suggest topics for selected</button>
      <button class="btn" id="regenTopics">Suggest new topics for selected</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" class="check-all"></th><th>Website</th><th>Niche</th><th style="min-width:360px">Topic</th></tr></thead>
      <tbody>${contacts.length ? contacts.map((c) => {
        const opts = c.topic_options || [];
        const custom = c.selected_topic && !opts.includes(c.selected_topic);
        return `<tr>
          <td><input type="checkbox" class="row-check" value="${c.id}"></td>
          <td class="wrap"><b>${esc(c.website_name)}</b><br><span class="small muted">${esc(c.email)}</span></td>
          <td>${esc(c.niche_label || c.niche)}</td>
          <td>${opts.map((t, i) => `<label class="topic-opt"><input type="radio" name="t${c.id}" value="${esc(t)}" data-cid="${c.id}" ${c.selected_topic === t ? "checked" : ""}> ${esc(t)}</label>`).join("") ||
            "<span class='muted small'>No suggestions yet. Select the row and click “Suggest topics”.</span>"}
            <div class="toolbar" style="margin:6px 0 0">
              <input type="text" placeholder="Or type your own topic" data-custom="${c.id}" value="${custom ? esc(c.selected_topic) : ""}">
              <button class="btn sm" data-savecustom="${c.id}">Use my topic</button>
            </div></td>
        </tr>`;
      }).join("") : emptyRow(4, "No contacts available.")}</tbody></table></div>`;
  wireSelectAll();
  const gen = (regenerate) => run(async () => {
    const ids = selectedIds();
    if (!ids.length) throw new Error("Select at least one website.");
    await api("/api/topics/generate", { method: "POST", body: { ids, regenerate } });
    toast("Topics updated.");
    route();
  });
  $("#genTopics").onclick = () => gen(false);
  $("#regenTopics").onclick = () => gen(true);
  $$("input[type=radio][data-cid]").forEach((r) => (r.onchange = () => saveTopic(r.dataset.cid, r.value)));
  $$("[data-savecustom]").forEach((b) => (b.onclick = () => {
    const val = $(`[data-custom="${b.dataset.savecustom}"]`).value.trim();
    if (!val) return toast("Type a topic first.", true);
    saveTopic(b.dataset.savecustom, val).then(route);
  }));
};

function saveTopic(cid, topic) {
  return run(async () => {
    await api(`/api/contacts/${cid}`, { method: "PUT", body: { selected_topic: topic } });
    toast("Topic selected.");
  });
}

// ------------------------------------------------------------------ 5. generate

pages.generate = async () => {
  const contacts = await api("/api/contacts");
  const ready = contacts.filter((c) => ["new", "drafted"].includes(c.outreach_status) && !c.suppressed);
  main.innerHTML = `
    <h1>Generate email drafts</h1>
    <p class="lead">Select up to ${MAX_BATCH} contacts. Each gets a separate, personalized draft. <b>Generating never sends anything.</b></p>
    <div class="toolbar">
      <button class="btn primary" id="genDrafts">Generate drafts for selected</button>
      <button class="btn" id="pick30">Select first ${MAX_BATCH} without a draft</button>
      <span class="small muted" id="selCount">0 selected</span>
    </div>
    <div id="genResult"></div>
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" class="check-all"></th><th>Website</th><th>Email</th><th>Selected topic</th><th>Draft</th><th>Issues</th></tr></thead>
      <tbody>${ready.length ? ready.map((c) => `<tr>
        <td><input type="checkbox" class="row-check" value="${c.id}"></td>
        <td class="wrap"><b>${esc(c.website_name)}</b></td>
        <td class="wrap">${esc(c.email)}</td>
        <td class="wrap">${esc(c.selected_topic) || "<span class='muted small'>Auto (first suggestion)</span>"}</td>
        <td>${c.active_draft_id ? pill("edited", "has draft") : pill("not_sent", "none")}</td>
        <td>${c.issues.length ? `<ul class="issues">${c.issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}</td>
      </tr>`).join("") : emptyRow(6, "No contacts ready for drafting.")}</tbody></table></div>`;
  wireSelectAll();
  const updateCount = () => ($("#selCount").textContent = `${selectedIds().length} selected (max ${MAX_BATCH})`);
  $("table", main).addEventListener("change", updateCount);
  $("#pick30").onclick = () => {
    let n = 0;
    $$("input.row-check").forEach((c) => {
      const row = ready.find((r) => r.id === Number(c.value));
      c.checked = !row.active_draft_id && n < MAX_BATCH && !row.issues.some((i) => i.startsWith("Invalid") || i.startsWith("Missing website name")) && ++n > 0;
    });
    updateCount();
  };
  $("#genDrafts").onclick = () => run(async () => {
    const ids = selectedIds();
    if (!ids.length) throw new Error("Select at least one contact.");
    if (ids.length > MAX_BATCH) throw new Error(`Select at most ${MAX_BATCH} contacts.`);
    const r = await api("/api/drafts/generate", { method: "POST", body: { ids } });
    $("#genResult").innerHTML = `<div class="banner ${r.created.length ? "ok" : "warn"}">
      Created <b>${r.created.length}</b> draft(s). ${r.created.length ? `<a href="#review">Review them →</a>` : ""}
      ${r.skipped.length ? `<br>Skipped ${r.skipped.length}:<ul class="small">${r.skipped.map((s) => `<li>${esc(s.email || "#" + s.contact_id)}: ${esc(s.reason)}</li>`).join("")}</ul>` : ""}
    </div>${(r.warnings || []).map((w) => `<div class="banner warn">${esc(w)}</div>`).join("")}`;
  });
};

// ------------------------------------------------------------------ 6. review

function draftRow(d, extraCols = "") {
  const locked = ["sending", "sent", "uncertain", "queued"].includes(d.send_status);
  return `<tr>
    <td><input type="checkbox" class="row-check" value="${d.id}" ${locked ? "disabled" : ""}></td>
    <td class="wrap"><b>${esc(d.website_name)}</b></td>
    <td class="wrap">${esc(d.to_email)}</td>
    <td class="wrap">${esc(d.subject)}</td>
    <td class="wrap small">${esc(d.topic)}</td>
    ${extraCols}
    <td>${pill(d.draft_status)}</td>
    <td>${pill(d.approval_status)}</td>
    <td>${pill(d.send_status)}</td>
    <td><div class="actions">
      <button class="btn sm" data-preview="${d.id}">Preview</button>
      <button class="btn sm" data-editdraft="${d.id}" ${locked ? "disabled" : ""}>Edit</button>
      <button class="btn sm ok" data-approve="${d.id}" ${locked || d.approval_status === "approved" ? "disabled" : ""}>Approve</button>
      <button class="btn sm danger" data-reject="${d.id}" ${locked || d.approval_status === "rejected" ? "disabled" : ""}>Reject</button>
      <button class="btn sm danger" data-deldraft="${d.id}" ${locked ? "disabled" : ""}>Delete</button>
    </div></td>
  </tr>`;
}

function wireDraftActions(onChange) {
  $$("[data-preview]").forEach((b) => (b.onclick = () => previewDraft(Number(b.dataset.preview))));
  $$("[data-editdraft]").forEach((b) => (b.onclick = () => editDraft(Number(b.dataset.editdraft), onChange)));
  $$("[data-approve]").forEach((b) => (b.onclick = () => approve([Number(b.dataset.approve)], onChange)));
  $$("[data-reject]").forEach((b) => (b.onclick = () => reject([Number(b.dataset.reject)], onChange)));
  $$("[data-deldraft]").forEach((b) => (b.onclick = () => run(async () => {
    if (!confirm("Delete this draft permanently?")) return;
    await api(`/api/drafts/${b.dataset.deldraft}`, { method: "DELETE" });
    toast("Draft deleted.");
    onChange();
  })));
}

async function approve(ids, onChange) {
  if (!ids.length) return toast("Select drafts first.", true);
  const r = await run(() => api("/api/drafts/approve", { method: "POST", body: { ids } }));
  if (!r) return;
  toast(`Approved ${r.approved.length}.` + (r.refused.length ? ` ${r.refused.length} refused: ${r.refused.map((x) => x.reason).join("; ")}` : ""), r.refused.length > 0);
  onChange();
}

async function reject(ids, onChange) {
  if (!ids.length) return toast("Select drafts first.", true);
  await run(() => api("/api/drafts/reject", { method: "POST", body: { ids } }));
  toast("Rejected.");
  onChange();
}

async function previewDraft(id) {
  const d = await run(() => api(`/api/drafts/${id}`));
  if (!d) return;
  openModal("Email preview", `
    <div class="meta-row"><b>To:</b> ${esc(d.to_email)}</div>
    <div class="meta-row"><b>Subject:</b> ${esc(d.subject)}</div>
    <div class="meta-row">${pill(d.draft_status)} ${pill(d.approval_status)} ${pill(d.send_status)}
      ${d.kind === "followup" && d.due_at ? `<span class="small muted">Due ${fmtDate(d.due_at)}</span>` : ""}</div>
    <div class="preview" style="margin-top:10px">${esc(d.body)}</div>
    ${d.notes ? `<p class="small muted"><b>Your notes:</b> ${esc(d.notes)}</p>` : ""}
    <details style="margin-top:12px"><summary class="small">Approval history</summary>
      <ul class="small">${d.history.map((h) => `<li>${fmtDate(h.created_at)}: ${esc(h.action.replace(/_/g, " "))} ${esc(h.note)}</li>`).join("")}</ul>
    </details>`);
}

async function editDraft(id, onChange) {
  const d = await run(() => api(`/api/drafts/${id}`));
  if (!d) return;
  openModal("Edit email", `
    ${d.approval_status === "approved" ? `<div class="banner warn">This draft is approved. Saving changes will remove the approval, and you'll need to approve it again.</div>` : ""}
    <div class="meta-row"><b>To:</b> ${esc(d.to_email)}</div>
    <div class="field"><label>Subject</label><input type="text" id="e_subject" value="${esc(d.subject)}"></div>
    <div class="field"><label>Topic</label><input type="text" id="e_topic" value="${esc(d.topic)}"></div>
    <div class="field"><label>Body</label><textarea class="body" id="e_body">${esc(d.body)}</textarea></div>
    ${d.notes ? `<p class="small muted"><b>Your notes for this site:</b> ${esc(d.notes)}</p>` : ""}
    <div class="toolbar"><button class="btn primary" id="saveDraft">Save changes</button><button class="btn" onclick="closeModal()">Cancel</button></div>`);
  $("#saveDraft").onclick = () => run(async () => {
    const r = await api(`/api/drafts/${id}`, { method: "PUT", body: { subject: $("#e_subject").value, body: $("#e_body").value, topic: $("#e_topic").value } });
    closeModal();
    toast(d.approval_status === "approved" && r.approval_status !== "approved" ? "Saved. Approval removed, so please review and approve again." : "Saved.");
    onChange();
  });
}

pages.review = async () => {
  const drafts = await api("/api/drafts?kind=initial");
  const filter = pages.review.filter || "pending";
  const shown = drafts.filter((d) => filter === "all" ? true :
    filter === "unsent" ? !["sent", "uncertain"].includes(d.send_status) :
    filter === "sent" ? ["sent", "uncertain"].includes(d.send_status) : d.approval_status === filter);
  main.innerHTML = `
    <h1>Review &amp; approve emails</h1>
    <p class="lead">Preview and edit every email. Only approved drafts can be sent, and editing an approved draft removes its approval.</p>
    <div class="toolbar">
      <select id="rfilter" style="width:auto">
        <option value="pending">Awaiting approval</option><option value="approved">Approved</option>
        <option value="rejected">Rejected</option><option value="unsent">All unsent</option><option value="sent">Sent</option><option value="all">All</option>
      </select>
      <span class="spacer"></span>
      <button class="btn ok" id="approveSel">Approve selected</button>
      <button class="btn" id="approveAll">Approve all displayed (${shown.filter((d) => d.approval_status !== "approved" && !["sent","uncertain","queued","sending"].includes(d.send_status)).length})</button>
      <button class="btn danger" id="rejectSel">Reject selected</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" class="check-all"></th><th>Website</th><th>Recipient</th><th>Subject</th><th>Topic</th>
      <th>Draft</th><th>Approval</th><th>Sending</th><th>Actions</th></tr></thead>
      <tbody>${shown.length ? shown.map((d) => draftRow(d)).join("") : emptyRow(9, "No drafts in this view.")}</tbody></table></div>`;
  $("#rfilter").value = filter;
  $("#rfilter").onchange = (e) => { pages.review.filter = e.target.value; route(); };
  wireSelectAll();
  wireDraftActions(route);
  $("#approveSel").onclick = () => approve(selectedIds(), route);
  $("#rejectSel").onclick = () => reject(selectedIds(), route);
  $("#approveAll").onclick = () => {
    const ids = shown.filter((d) => d.approval_status !== "approved" && !["sent", "uncertain", "queued", "sending"].includes(d.send_status)).map((d) => d.id);
    if (!ids.length) return toast("Nothing to approve in this view.", true);
    if (!confirm(`Approve all ${ids.length} displayed draft(s)? Make sure you've read each one.`)) return;
    approve(ids, route);
  };
};

// ------------------------------------------------------------------ 7. queue

pages.queue = async () => {
  const [initial, followups, batches, stats] = await Promise.all([
    api("/api/drafts?kind=initial"), api("/api/drafts?kind=followup"), api("/api/send/batches"), api("/api/stats")]);
  const now = new Date().toISOString();
  const ready = [...initial, ...followups].filter((d) => d.approval_valid && ["not_sent", "failed"].includes(d.send_status));
  const active = batches.find((b) => ["sending", "awaiting_confirmation"].includes(b.status));
  main.innerHTML = `
    <h1>Sending queue</h1>
    <p class="lead">Select approved drafts, prepare a batch (max ${MAX_BATCH}), check the confirmation screen, then confirm.</p>
    ${modeBanner(stats.mode)}
    <div id="activeBatch"></div>
    <div class="toolbar">
      <button class="btn primary" id="prepare">Prepare batch from selected</button>
      <span class="small muted">Sent in last 24h: ${stats.sent_last_24h} / ${stats.daily_limit}</span>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" class="check-all"></th><th>Website</th><th>Recipient</th><th>Subject</th><th>Type</th><th>Status</th></tr></thead>
      <tbody>${ready.length ? ready.map((d) => {
        const notDue = d.kind === "followup" && d.due_at && d.due_at > now;
        return `<tr>
          <td><input type="checkbox" class="row-check" value="${d.id}" ${notDue ? "disabled" : ""}></td>
          <td class="wrap">${esc(d.website_name)}</td><td class="wrap">${esc(d.to_email)}</td><td class="wrap">${esc(d.subject)}</td>
          <td>${d.kind === "followup" ? pill("edited", "follow-up") : "initial"}${notDue ? `<br><span class="small muted">due ${fmtDate(d.due_at)}</span>` : ""}</td>
          <td>${pill("approved")} ${d.send_status === "failed" ? pill("failed", "previous attempt failed") : ""}</td>
        </tr>`;
      }).join("") : emptyRow(6, "No approved drafts waiting. Approve drafts on the Review page first.")}</tbody></table></div>

    <h2>Recent batches</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>#</th><th>Created</th><th>Mode</th><th>Status</th><th>Results</th><th>Note</th><th></th></tr></thead>
      <tbody>${batches.length ? batches.map((b) => `<tr>
        <td>${b.id}</td><td>${fmtDate(b.created_at)}</td><td>${esc(b.mode.replace("_", " "))}</td><td>${pill(b.status)}</td>
        <td class="small">${Object.entries(b.counts).map(([k, v]) => `${esc(k)}: ${v}`).join(" · ")}</td>
        <td class="wrap small">${esc(b.stop_reason)}</td>
        <td><button class="btn sm" data-batch="${b.id}">Details</button></td></tr>`).join("") : emptyRow(7, "No batches yet.")}</tbody></table></div>`;
  wireSelectAll();
  $$("[data-batch]").forEach((b) => (b.onclick = () => batchDetails(Number(b.dataset.batch))));
  $("#prepare").onclick = () => run(async () => {
    const ids = selectedIds();
    if (!ids.length) throw new Error("Select approved drafts first.");
    if (ids.length > MAX_BATCH) throw new Error(`Select at most ${MAX_BATCH}.`);
    const r = await api("/api/send/prepare", { method: "POST", body: { ids } });
    if (!r.batch) {
      openModal("Nothing can be sent", `<p>None of the selected drafts passed the safety checks:</p>
        <ul>${r.excluded.map((x) => `<li>${esc(x.to_email || "#" + x.draft_id)}: ${esc(x.reasons.join("; "))}</li>`).join("")}</ul>`);
      return;
    }
    showConfirmation(r);
  });
  if (active) renderActive(active.id);
};

function showConfirmation(r) {
  const b = r.batch;
  openModal(`Final confirmation: batch #${b.id}`, `
    ${modeBanner(b.mode, b.test_recipient)}
    <p><b>${r.items.length}</b> email(s) will be sent one at a time, ${b.delay_seconds} seconds apart.</p>
    ${r.excluded.length ? `<div class="banner warn"><b>${r.excluded.length} draft(s) left out:</b>
      <ul class="small">${r.excluded.map((x) => `<li>${esc(x.to_email || "#" + x.draft_id)}: ${esc(x.reasons.join("; "))}</li>`).join("")}</ul></div>` : ""}
    <div class="confirm-list">${r.items.map((i, n) => `
      <div class="confirm-item">
        <div class="meta-row"><b>${n + 1}. To:</b> ${esc(i.to_email)}${b.mode === "test" ? ` <span class="small muted">(test copy goes to ${esc(b.test_recipient)})</span>` : ""}</div>
        <div class="meta-row"><b>Subject:</b> ${esc(i.subject)}</div>
        <details><summary>Show message</summary><div class="preview">${esc(i.body)}</div></details>
      </div>`).join("")}</div>
    <div class="field" style="margin-top:14px">
      <label for="confirmText">Type <b>SEND</b> to confirm you have reviewed these emails</label>
      <input type="text" id="confirmText" autocomplete="off" style="max-width:200px">
    </div>
    <div class="toolbar">
      <button class="btn ${b.mode === "live" ? "danger solid" : "primary"}" id="confirmSend" disabled>Start sending ${r.items.length} email(s)</button>
      <button class="btn" id="cancelBatch">Cancel batch</button>
    </div>`);
  $("#confirmText").oninput = (e) => ($("#confirmSend").disabled = e.target.value.trim() !== "SEND");
  $("#cancelBatch").onclick = () => run(async () => {
    await api(`/api/send/batches/${b.id}/cancel`, { method: "POST" });
    closeModal();
    toast("Batch cancelled. Nothing was sent.");
    route();
  });
  $("#confirmSend").onclick = () => run(async () => {
    $("#confirmSend").disabled = true;
    await api("/api/send/confirm", { method: "POST", body: { batch_id: b.id, token: b.token, confirm_text: $("#confirmText").value } });
    closeModal();
    toast("Sending started.");
    route();
  });
}

async function renderActive(batchId) {
  const box = $("#activeBatch");
  if (!box) return;
  const r = await api(`/api/send/batches/${batchId}`).catch(() => null);
  if (!r) return;
  const b = r.batch;
  const done = r.items.filter((i) => !["queued", "sending"].includes(i.status)).length;
  box.innerHTML = `<div class="card">
    <div class="page-head"><h3>Batch #${b.id}: ${pill(b.status)}</h3>
      ${["sending", "awaiting_confirmation"].includes(b.status) ? `<button class="btn danger solid" id="cancelSending">Cancel sending</button>` : ""}</div>
    <div class="progress"><div style="width:${r.items.length ? (100 * done) / r.items.length : 0}%"></div></div>
    <p class="small muted">${done} of ${r.items.length} processed. ${b.status === "awaiting_confirmation" ? "This batch has not been confirmed, so nothing has been sent." : ""}</p>
    <div class="table-wrap"><table><thead><tr><th>Recipient</th><th>Subject</th><th>Status</th><th>Note</th></tr></thead><tbody>
      ${r.items.map((i) => `<tr><td class="wrap">${esc(i.to_email)}</td><td class="wrap">${esc(i.subject)}</td><td>${pill(i.status)}</td><td class="wrap small">${esc(i.note)}</td></tr>`).join("")}
    </tbody></table></div></div>`;
  const cancel = $("#cancelSending");
  if (cancel) cancel.onclick = () => run(async () => {
    if (!confirm("Stop sending? Emails not yet sent will be cancelled.")) return;
    await api(`/api/send/batches/${b.id}/cancel`, { method: "POST" });
    toast("Cancelled. Remaining emails will not be sent.");
    route();
  });
  clearInterval(pollTimer);
  if (b.status === "sending") pollTimer = setInterval(() => renderActive(batchId), 2000);
  else if (b.status !== "awaiting_confirmation") { /* finished: leave final state visible */ }
}

async function batchDetails(id) {
  const r = await run(() => api(`/api/send/batches/${id}`));
  if (!r) return;
  openModal(`Batch #${id}`, `
    <p>${pill(r.batch.status)} Mode: ${esc(r.batch.mode.replace("_", " "))} · Provider: ${esc(r.batch.provider)}</p>
    ${r.batch.stop_reason ? `<div class="banner warn">${esc(r.batch.stop_reason)}</div>` : ""}
    <div class="table-wrap"><table><thead><tr><th>Recipient</th><th>Subject</th><th>Status</th><th>Note</th></tr></thead><tbody>
    ${r.items.map((i) => `<tr><td class="wrap">${esc(i.to_email)}</td><td class="wrap">${esc(i.subject)}</td><td>${pill(i.status)}</td><td class="wrap small">${esc(i.note)}</td></tr>`).join("")}
    </tbody></table></div>`);
}

// ------------------------------------------------------------------ 8. history

pages.history = async () => {
  const log = await api("/api/history");
  main.innerHTML = `
    <h1>Sent email history</h1>
    <p class="lead">Every send attempt, including test copies and dry runs. Uncertain sends are never retried automatically.
    Check your email account's Sent folder, then record what happened.</p>
    <div class="table-wrap"><table>
      <thead><tr><th>Time</th><th>Recipient</th><th>Subject</th><th>Mode</th><th>Result</th><th>Provider response</th><th></th></tr></thead>
      <tbody>${log.length ? log.map((l) => `<tr>
        <td class="small">${fmtDate(l.created_at)}</td>
        <td class="wrap">${esc(l.recipient)}${l.actual_recipient.toLowerCase() !== l.recipient.toLowerCase() ? `<br><span class="small muted">delivered to ${esc(l.actual_recipient)}</span>` : ""}</td>
        <td class="wrap">${esc(l.subject)}</td>
        <td>${esc(l.mode.replace("_", " "))}${l.kind === "followup" ? "<br><span class='small muted'>follow-up</span>" : ""}</td>
        <td>${pill(l.result)}${l.resolved ? `<br><span class="small muted">marked ${esc(l.resolved.replace("_", " "))}</span>` : ""}</td>
        <td class="wrap small">${esc(l.provider_response)}</td>
        <td>${l.result === "uncertain" && !l.resolved ? `<div class="actions">
          <button class="btn sm" data-resolve="${l.id}" data-res="sent">It was sent</button>
          <button class="btn sm" data-resolve="${l.id}" data-res="not_sent">It was not sent</button></div>` : ""}</td>
      </tr>`).join("") : emptyRow(7, "Nothing sent yet.")}</tbody></table></div>`;
  $$("[data-resolve]").forEach((b) => (b.onclick = () => run(async () => {
    const msg = b.dataset.res === "sent" ? "Mark this email as sent?" :
      "Mark as NOT sent? The draft will need to be approved again before it can be resent.";
    if (!confirm(msg)) return;
    await api(`/api/history/${b.dataset.resolve}/resolve`, { method: "POST", body: { resolution: b.dataset.res } });
    route();
  })));
};

// ------------------------------------------------------------------ 9. follow-ups

pages.followups = async () => {
  const [data, settings, alertsPart] = await Promise.all([api("/api/followups"), api("/api/settings"), alertsSection()]);
  const enabled = settings.followups_enabled === "true";
  main.innerHTML = `
    <h1>Follow-up manager</h1>
    <p class="lead">Optional, one follow-up per email at most. Each follow-up needs its own approval and confirmation before sending.
    Pending follow-ups are cancelled automatically when a contact replies, opts out or is marked do-not-contact.</p>
    ${enabled ? "" : `<div class="banner warn">Follow-ups are <b>disabled</b>. Turn them on in <a href="#settings">Settings</a> to schedule any.</div>`}
    ${alertsPart.html}
    <h2>Sent emails</h2>
    <div class="toolbar">
      <label for="fdelay" style="margin:0">Follow up after</label>
      <input type="number" id="fdelay" min="1" max="60" value="${esc(settings.followup_delay_days)}" style="width:80px"> days
      <button class="btn primary" id="makeFollowups" ${enabled ? "" : "disabled"}>Create follow-up drafts for selected</button>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th><input type="checkbox" class="check-all"></th><th>Website</th><th>Recipient</th><th>Subject</th><th>Sent</th><th>Contact status</th><th>Reply</th><th>Follow-up</th></tr></thead>
      <tbody>${data.sent_originals.length ? data.sent_originals.map((d) => `<tr>
        <td><input type="checkbox" class="row-check" value="${d.id}" ${d.followups ? "disabled" : ""}></td>
        <td class="wrap">${esc(d.website_name)}</td><td class="wrap">${esc(d.to_email)}</td><td class="wrap">${esc(d.subject)}</td>
        <td class="small">${fmtDate(d.sent_at)}</td><td>${pill(d.outreach_status || "removed")}</td>
        <td>${d.reply_id ? `<button class="btn sm ${d.reply_kind === "bounce" ? "danger" : "primary"}" data-viewreply="${d.reply_id}">${d.reply_kind === "bounce" ? "View bounce" : "View reply"}</button>` : `<span class="muted small">No reply yet</span>`}</td>
        <td>${d.followups ? pill("edited", "created") : "—"}</td></tr>`).join("") : emptyRow(8, "No live emails sent yet.")}</tbody></table></div>
    <p class="small muted">Replies are detected automatically (see Replies &amp; alerts above) and cancel that contact's follow-up.
      For a reply the app missed, mark the contact as <i>replied</i> in <a href="#contacts">Manage contacts</a>.</p>

    <h2>Follow-up drafts</h2>
    <div class="toolbar"><button class="btn ok" id="fApprove">Approve selected</button><button class="btn danger" id="fReject">Reject selected</button>
      <button class="btn danger" id="fDelete">Delete selected</button></div>
    <div class="table-wrap" id="fdrafts"><table>
      <thead><tr><th><input type="checkbox" class="check-all"></th><th>Website</th><th>Recipient</th><th>Subject</th><th>Topic</th><th>Due</th>
      <th>Draft</th><th>Approval</th><th>Sending</th><th>Actions</th></tr></thead>
      <tbody>${data.followups.length ? data.followups.map((d) => draftRow(d, `<td class="small">${fmtDate(d.due_at)}</td>`)).join("") : emptyRow(10, "No follow-ups.")}</tbody></table></div>
    <p class="small muted">Approved follow-ups appear in the <a href="#queue">Sending queue</a> once they are due.</p>`;
  $$("input.check-all").forEach((all) => {
    const table = all.closest("table");
    all.onchange = () => $$("input.row-check", table).forEach((c) => { if (!c.disabled) c.checked = all.checked; });
  });
  wireDraftActions(route);
  alertsPart.wire();
  const fdraftsTable = $("#fdrafts");
  $("#fApprove").onclick = () => approve(selectedIds(fdraftsTable), route);
  $("#fReject").onclick = () => reject(selectedIds(fdraftsTable), route);
  $("#fDelete").onclick = () => run(async () => {
    const ids = selectedIds(fdraftsTable);
    if (!ids.length) throw new Error("Select follow-up drafts first.");
    if (!confirm(`Delete ${ids.length} follow-up draft(s) permanently? This can't be undone. Sent emails are never deleted.`)) return;
    const r = await api("/api/drafts/delete", { method: "POST", body: { ids } });
    toast(`Deleted ${r.deleted.length} follow-up draft(s).` + (r.skipped.length ? ` Kept ${r.skipped.length}: ${r.skipped[0].reason}` : ""), r.skipped.length > 0);
    route();
  });
  $("#makeFollowups").onclick = () => run(async () => {
    const ids = $$("input.row-check:checked").filter((c) => !fdraftsTable.contains(c)).map((c) => Number(c.value));
    if (!ids.length) throw new Error("Select sent emails first.");
    const r = await api("/api/followups/generate", { method: "POST", body: { ids, delay_days: Number($("#fdelay").value) } });
    toast(`Created ${r.created.length} follow-up draft(s).` + (r.skipped.length ? ` Skipped: ${r.skipped.map((s) => s.reason).join("; ")}` : ""), r.skipped.length > 0);
    route();
  });
};

// ------------------------------------------------------------------ 10. settings

pages.settings = async () => {
  const s = await api("/api/settings");
  const g = s.gmail;
  main.innerHTML = `
    <h1>Settings &amp; email account</h1>
    <p class="lead">Secrets (passwords, OAuth client files) live in your <code>.env</code> file and the <code>credentials</code> folder, never here.</p>

    <div class="card">
      <h3>Email provider</h3>
      <div class="grid2" style="margin-top:12px">
        <div class="field"><label>Provider</label><select id="s_provider">
          <option value="dryrun">Dry run (sends nothing)</option>
          <option value="gmail">Gmail API (OAuth 2.0)</option>
          <option value="smtp">SMTP (alternative)</option></select></div>
        <div class="field"><label>Test recipient (your own address)</label><input type="email" id="s_test_recipient" value="${esc(s.test_recipient)}"></div>
      </div>
      <label class="check"><input type="checkbox" id="s_test_mode" ${s.test_mode === "true" ? "checked" : ""}>
        Test mode: send every email only to my test recipient</label>
      <p class="hint">Keep test mode on until you've checked how emails look in your own inbox.</p>
      <div style="margin-top:12px">
        <b>Gmail:</b> ${g.connected ? pill("approved", "connected") : pill("pending", "not connected")}
        ${g.client_secret_present ? "" : `<span class="small muted">: OAuth client file not found (see README).</span>`}
        ${g.connecting ? `<span class="small muted">Waiting for you to finish in the browser…</span>` : ""}
        ${g.error ? `<div class="banner bad small">${esc(g.error)}</div>` : ""}
        <div class="toolbar" style="margin-top:6px">
          <button class="btn" id="gConnect" ${g.client_secret_present ? "" : "disabled"}>${g.connected ? "Reconnect" : "Connect"} Gmail</button>
          ${g.connected ? `<button class="btn danger" id="gDisconnect">Disconnect</button>` : ""}
          <button class="btn" id="gRefresh">Refresh status</button>
        </div>
        <b>SMTP:</b> ${s.smtp_configured ? pill("approved", "configured in .env") : pill("pending", "not configured")}
      </div>
    </div>

    <div class="card">
      <h3>Sender identity</h3>
      <div class="grid2" style="margin-top:12px">
        <div class="field"><label>Your name</label><input type="text" id="s_sender_name" value="${esc(s.sender_name)}"></div>
        <div class="field"><label>Your email (shown in signature)</label><input type="email" id="s_sender_email" value="${esc(s.sender_email)}"></div>
        <div class="field"><label>Website / portfolio (optional)</label><input type="text" id="s_sender_website" value="${esc(s.sender_website)}"></div>
        <div class="field"><label>Postal address (optional; required by some anti-spam laws)</label><input type="text" id="s_sender_postal_address" value="${esc(s.sender_postal_address)}"></div>
      </div>
      <div class="field"><label>Professional description</label><textarea id="s_sender_description">${esc(s.sender_description)}</textarea></div>
    </div>

    <div class="card">
      <h3>Sending limits</h3>
      <div class="grid2" style="margin-top:12px">
        <div class="field"><label>Delay between emails (seconds)</label><input type="number" id="s_send_delay_seconds" min="5" max="600" value="${esc(s.send_delay_seconds)}"></div>
        <div class="field"><label>Daily sending limit (24h)</label><input type="number" id="s_daily_send_limit" min="1" max="2000" value="${esc(s.daily_send_limit)}">
          <div class="hint">Set this at or below your provider's limit. Batches are always capped at ${s.max_batch}.</div></div>
      </div>
    </div>

    <div class="card">
      <h3>Follow-ups</h3>
      <label class="check"><input type="checkbox" id="s_followups_enabled" ${s.followups_enabled === "true" ? "checked" : ""}> Enable optional follow-ups</label>
      <div class="field" style="max-width:240px;margin-top:8px"><label>Default follow-up delay (days)</label>
        <input type="number" id="s_followup_delay_days" min="1" max="60" value="${esc(s.followup_delay_days)}"></div>
    </div>
    <button class="btn primary" id="saveSettings">Save settings</button>

    <div class="card" style="margin-top:18px">
      <h3>Backup &amp; move data</h3>
      <p class="small">Download a full copy of your data (contacts, drafts, sent history, replies, websites). To move to the hosted app,
        download it here on your computer, then upload it on the hosted app. The data already there is saved first.</p>
      <div class="toolbar"><a class="btn" href="/api/admin/backup.db">Download backup (.db)</a></div>
      <div class="toolbar" style="margin-top:8px">
        <input type="file" id="restoreFile" accept=".db">
        <input type="text" id="restoreConfirm" placeholder="Type REPLACE" style="width:140px">
        <button class="btn danger" id="restoreGo">Replace data with this backup</button>
      </div>
    </div>`;
  $("#restoreGo").onclick = () => run(async () => {
    const f = $("#restoreFile").files[0];
    if (!f) throw new Error("Choose a .db backup file first.");
    if (!confirm("Replace ALL data in this app with the uploaded backup? The current data is saved first.")) return;
    const fd = new FormData();
    fd.append("file", f); fd.append("confirm_text", $("#restoreConfirm").value);
    const r = await api("/api/admin/restore", { method: "POST", body: fd });
    toast(`Restored: ${r.counts.contacts} contacts, ${r.counts.drafts} drafts, ${r.counts.websites} websites. Previous data saved as ${r.previous_saved_as}.`);
    route();
  });
  $("#s_provider").value = s.provider;
  $("#saveSettings").onclick = () => run(async () => {
    const body = {
      provider: $("#s_provider").value,
      test_mode: $("#s_test_mode").checked,
      followups_enabled: $("#s_followups_enabled").checked,
      send_delay_seconds: Number($("#s_send_delay_seconds").value),
      daily_send_limit: Number($("#s_daily_send_limit").value),
      followup_delay_days: Number($("#s_followup_delay_days").value),
    };
    ["test_recipient", "sender_name", "sender_email", "sender_website", "sender_postal_address", "sender_description"]
      .forEach((k) => (body[k] = $(`#s_${k}`).value));
    if (body.provider !== "dryrun" && !body.test_mode &&
        !confirm("Test mode is OFF, so confirmed emails will go to real recipients. Save anyway?")) return;
    await api("/api/settings", { method: "PUT", body });
    toast("Settings saved.");
    route();
  });
  $("#gConnect").onclick = () => run(async () => {
    if (await connectGmail()) return;
    toast("A browser window will open. Sign in and allow 'Send email on your behalf'.");
    setTimeout(route, 1500);
  });
  const disc = $("#gDisconnect");
  if (disc) disc.onclick = () => run(async () => { await api("/api/gmail/disconnect", { method: "POST" }); route(); });
  $("#gRefresh").onclick = route;
};

route();
