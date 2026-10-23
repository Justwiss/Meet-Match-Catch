/* StudiRadar – Oberfläche (ohne Build-Schritt, reines JavaScript) */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];

const state = {
  data: null,
  view: "home",
  feedFilter: "all",
  dlFilter: "all",
  showDone: false,
  editing: null,
  klipsPoll: null,
};

const KIND_ICON = {
  file: "📄", deadline: "⏳", exercise: "📝", test: "✅", forum: "💬", news: "📰",
  grade: "🎓", exam: "🧪", task: "📌", event: "📅", webr: "🔗", wiki: "📚", xvid: "🎬",
  xopc: "🎬", copa: "📃", svy: "📊", klips: "🏛️", lm: "📘", sahs: "📘", htlm: "📘",
};
const KIND_NAME = {
  file: "Datei", deadline: "Frist", exercise: "Übung", test: "Test", forum: "Forum", news: "News",
  grade: "KLIPS", exam: "Prüfung", task: "Eigene Aufgabe", event: "Termin", webr: "Link",
};
const VIEW_TITLE = { home: "Übersicht", updates: "Neuigkeiten", deadlines: "Fristen & Aufgaben", more: "Mehr" };

// ---------- Hilfen ----------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("Nicht angemeldet");
  }
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || `Fehler ${res.status}`);
  return json;
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

function parseDate(iso) { return iso ? new Date(iso) : null; }

function dayKey(d) { return d.toISOString().slice(0, 10); }

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function fmtTime(d) { return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }); }

function fmtDay(d) {
  const today = new Date();
  const tomorrow = new Date(); tomorrow.setDate(today.getDate() + 1);
  const yesterday = new Date(); yesterday.setDate(today.getDate() - 1);
  if (sameDay(d, today)) return "Heute";
  if (sameDay(d, tomorrow)) return "Morgen";
  if (sameDay(d, yesterday)) return "Gestern";
  return d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: d.getFullYear() !== today.getFullYear() ? "numeric" : undefined });
}

function dueChip(iso, done) {
  const d = parseDate(iso);
  if (!d) return "";
  const diffH = (d - new Date()) / 36e5;
  let cls = "", label;
  if (done) { cls = "green"; label = "erledigt"; }
  else if (diffH < 0) { cls = "red"; label = "überfällig"; }
  else if (diffH < 24) { cls = "red"; label = diffH < 1 ? `${Math.max(1, Math.round(diffH * 60))} Min` : `${Math.round(diffH)} Std`; }
  else if (diffH < 72) { cls = "orange"; label = `${Math.round(diffH / 24)} Tg`; }
  else { label = `${Math.round(diffH / 24)} Tg`; }
  return `<span class="due ${cls}" title="${esc(d.toLocaleString("de-DE"))}">${label}</span>`;
}

function relTime(iso) {
  const d = parseDate(iso);
  if (!d) return "";
  const mins = Math.round((new Date() - d) / 6e4);
  if (mins < 1) return "gerade eben";
  if (mins < 60) return `vor ${mins} Min`;
  if (mins < 60 * 24) return `vor ${Math.round(mins / 60)} Std`;
  return `${fmtDay(d)} ${fmtTime(d)}`;
}

// ---------- Darstellung einzelner Einträge ----------
function itemRow(it, { withCheck = false, showDue = true, showSeen = true } = {}) {
  const due = parseDate(it.due_at);
  const unread = showSeen && !it.seen && it.source !== "manual";
  const metaBits = [];
  if (it.course) metaBits.push(`<span class="course">${esc(it.course)}</span>`);
  if (due) metaBits.push(`${fmtDay(due)}, ${fmtTime(due)}`);
  if (it.detail) metaBits.push(esc(it.detail.length > 140 ? it.detail.slice(0, 140) + "…" : it.detail));
  const left = withCheck
    ? `<button class="check ${it.done ? "on" : ""}" data-act="toggle" aria-label="Erledigt"></button>`
    : `<span class="kind" title="${esc(KIND_NAME[it.kind] || it.kind)}">${KIND_ICON[it.kind] || "•"}</span>`;
  return `<li class="item ${unread ? "unread" : ""} ${it.done ? "done" : ""}" id="item-${it.id}" data-id="${it.id}">
      ${left}
      <div class="body" data-act="open">
        <div class="title">${esc(it.title)}</div>
        <div class="meta">${metaBits.join(" · ")}</div>
      </div>
      ${showDue && due ? dueChip(it.due_at, it.done) : ""}
    </li>`;
}

function findItem(id) {
  const d = state.data;
  if (!d) return null;
  return [...d.deadlines, ...d.feed, ...d.notes, ...d.events].find((x) => x.id === id) || null;
}

// ---------- Ansichten ----------
function renderHome() {
  const d = state.data;
  const now = new Date();
  const upcoming = d.deadlines.filter((x) => !x.done && parseDate(x.due_at) > new Date(now - 864e5)).slice(0, 6);
  $("#home-deadlines").innerHTML = upcoming.length
    ? upcoming.map((x) => itemRow(x, { withCheck: true, showSeen: false })).join("")
    : `<li class="empty">Keine offenen Fristen 🎉</li>`;

  const fresh = d.feed.filter((x) => !x.seen).slice(0, 6);
  $("#home-new").innerHTML = fresh.length
    ? fresh.map((x) => itemRow(x, { showDue: false })).join("")
    : `<li class="empty">Nichts Neues – alles gelesen.</li>`;

  const ev = d.events.slice(0, 8);
  $("#home-events").innerHTML = ev.length
    ? ev.map((x) => itemRow(x, { showDue: false, showSeen: false })).join("")
    : `<li class="empty">${d.sources.ical.length ? "Keine Termine in den nächsten 7 Tagen." : "Tipp: Kalender-Abo-Link aus ILIAS/KLIPS in config.toml eintragen, dann erscheinen hier deine Termine."}</li>`;

  const ilias = d.syncs.find((s) => s.source === "ilias");
  $("#home-sync").textContent = ilias ? `ILIAS zuletzt abgerufen ${relTime(ilias.finished || ilias.started)}` : "";
  renderKlipsCard($("#klips-card-home"), true);
}

function renderFeed() {
  const f = state.feedFilter;
  let list = state.data.feed;
  if (f === "unread") list = list.filter((x) => !x.seen);
  else if (f === "klips") list = list.filter((x) => x.source.startsWith("klips") || x.source === "ical:klips");
  else if (f !== "all") list = list.filter((x) => x.kind === f);
  if (!list.length) {
    $("#feed").innerHTML = `<div class="card"><p class="empty">Hier ist (noch) nichts. Beim ersten Abruf merkt sich StudiRadar den aktuellen Stand – ab dann erscheint alles Neue hier.</p></div>`;
    return;
  }
  const groups = new Map();
  for (const it of list) {
    const key = fmtDay(parseDate(it.updated_at));
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(it);
  }
  $("#feed").innerHTML = [...groups].map(([day, items]) =>
    `<div class="group-title">${esc(day)}</div><div class="card"><ul class="list">${items.map((x) => itemRow(x)).join("")}</ul></div>`
  ).join("");
}

function renderDeadlines() {
  const d = state.data;
  let list = d.deadlines;
  if (state.dlFilter === "uni") list = list.filter((x) => x.source !== "manual");
  if (state.dlFilter === "manual") list = list.filter((x) => x.source === "manual");
  const now = new Date();
  const endOfToday = new Date(now); endOfToday.setHours(23, 59, 59, 999);
  const endOfWeek = new Date(endOfToday); endOfWeek.setDate(endOfWeek.getDate() + 7);
  const buckets = { "Überfällig": [], "Heute": [], "Nächste 7 Tage": [], "Später": [], "Erledigt": [] };
  for (const it of list) {
    const due = parseDate(it.due_at);
    if (it.done) { if (state.showDone) buckets["Erledigt"].push(it); continue; }
    if (due < now) buckets["Überfällig"].push(it);
    else if (due <= endOfToday) buckets["Heute"].push(it);
    else if (due <= endOfWeek) buckets["Nächste 7 Tage"].push(it);
    else buckets["Später"].push(it);
  }
  let notes = d.notes;
  if (state.dlFilter === "uni") notes = [];
  if (!state.showDone) notes = notes.filter((x) => !x.done);
  let html = "";
  for (const [name, items] of Object.entries(buckets)) {
    if (!items.length) continue;
    html += `<div class="group-title">${name}</div><div class="card"><ul class="list">${items.map((x) => itemRow(x, { withCheck: true, showSeen: false })).join("")}</ul></div>`;
  }
  if (notes.length) {
    html += `<div class="group-title">Ohne Datum</div><div class="card"><ul class="list">${notes.map((x) => itemRow(x, { withCheck: true, showSeen: false })).join("")}</ul></div>`;
  }
  $("#deadlines").innerHTML = html || `<div class="card"><p class="empty">Keine Fristen. Mit <b>+</b> kannst du eigene Aufgaben anlegen.</p></div>`;
}

const SOURCE_LABEL = { ilias: "ILIAS", "ilias-news": "ILIAS-News", ical: "Kalender-Abos", klips: "KLIPS" };

function renderMore() {
  const d = state.data;
  const rows = [];
  const sources = [];
  if (d.sources.ilias) sources.push("ilias");
  if (d.sources.ical.length) sources.push("ical");
  for (const src of sources) {
    const s = d.syncs.find((x) => x.source === src);
    const st = s ? s.status : "never";
    const dot = st === "running" ? "running" : st === "ok" ? "ok" : st === "warn" ? "warn" : st === "error" ? "error" : "";
    const when = s ? relTime(s.finished || s.started) : "noch nie";
    const info = s && s.status !== "running" ? ` · ${s.new_count} neu` : "";
    rows.push(`<li><div class="sync-row"><div><span class="status-dot ${dot}"></span><b>${SOURCE_LABEL[src]}</b>
      <div class="muted small">${esc(when)}${info}${src === "ical" ? " · " + esc(d.sources.ical.join(", ")) : ""}</div></div>
      <button class="btn small" data-sync="${src}">Abrufen</button></div>
      ${s && s.message ? `<div class="sync-msg">${esc(s.message)}</div>` : ""}</li>`);
  }
  $("#sync-list").innerHTML = rows.join("") || `<li class="empty">Keine Quellen aktiviert (config.toml).</li>`;
  renderKlipsCard($("#klips-card"), false);
  renderPushState();
}

function renderKlipsCard(el, compact) {
  const d = state.data;
  if (!d.sources.klips) {
    el.innerHTML = compact ? "" : `<div class="card-head"><h2>KLIPS</h2></div><p class="muted">KLIPS ist deaktiviert. In config.toml unter [klips] enabled = true setzen.</p>`;
    if (compact) el.className = "";
    return;
  }
  const k = d.klips || {};
  const s = d.syncs.find((x) => x.source === "klips");
  const busy = ["starting", "login", "duo_wait", "syncing"].includes(k.state);
  if (compact && !busy && k.state !== "needs_user") { el.innerHTML = ""; el.className = ""; return; }
  el.className = "card";
  let body = "";
  if (k.state === "duo_wait") {
    body = `<p><b>Öffne Duo Mobile</b> und gib diese Nummer ein:</p>
      <div class="klips-code">${esc(k.code || "…")}</div>`;
  } else if (busy) {
    body = `<p><span class="status-dot running"></span>${esc(k.message || "Läuft …")}</p>`;
  } else if (k.state === "needs_user") {
    body = `<p>KLIPS braucht eine Duo-Bestätigung. Starte den Abruf, wenn du dein Handy griffbereit hast.</p>`;
  } else if (k.state === "error") {
    body = `<p class="error">${esc(k.message)}</p>`;
  } else {
    body = `<p class="muted">${s ? "Zuletzt: " + esc(relTime(s.finished || s.started)) + (s.message ? " – " + esc(s.message) : "") : "Noch nie abgerufen."}</p>
      <p class="muted small">Ablauf: Abruf starten → Nummer erscheint hier und als Push → in Duo Mobile eingeben. Danach merkt Duo den Pi ca. 9 Stunden.</p>`;
  }
  el.innerHTML = `<div class="card-head"><h2>KLIPS</h2></div>${body}
    ${busy ? "" : `<div class="row-btns"><button class="btn primary small" data-klips="start">KLIPS jetzt abrufen</button></div>`}`;
}

function render() {
  if (!state.data) return;
  const unread = state.data.unread;
  const badge = $("#badge");
  badge.hidden = !unread;
  badge.textContent = unread > 99 ? "99+" : unread;
  if (navigator.setAppBadge) {
    (unread ? navigator.setAppBadge(unread) : navigator.clearAppBadge()).catch(() => {});
  }
  const courses = new Set([...state.data.feed, ...state.data.deadlines].map((x) => x.course).filter(Boolean));
  $("#course-list").innerHTML = [...courses].map((c) => `<option value="${esc(c)}">`).join("");
  ({ home: renderHome, updates: renderFeed, deadlines: renderDeadlines, more: renderMore })[state.view]();
}

// ---------- Daten laden ----------
async function load() {
  try {
    state.data = await api("/api/overview");
    render();
    const k = state.data.klips;
    if (k && ["starting", "login", "duo_wait", "syncing"].includes(k.state)) startKlipsPoll();
  } catch (e) {
    if (e.message !== "Nicht angemeldet") toast(e.message);
  }
}

// ---------- Navigation ----------
function route() {
  const hash = location.hash.replace("#", "") || "home";
  let view = hash;
  let focus = null;
  if (hash.startsWith("item-")) {
    focus = Number(hash.slice(5));
    const it = findItem(focus);
    view = it && it.due_at && it.kind !== "event" ? "deadlines" : "updates";
  }
  if (hash === "klips") view = "more";
  if (!VIEW_TITLE[view]) view = "home";
  state.view = view;
  $$(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  $$(".tabbar a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  $("#view-title").textContent = VIEW_TITLE[view];
  $("#fab").hidden = view === "more";
  render();
  if (focus) {
    requestAnimationFrame(() => {
      const el = document.getElementById(`item-${focus}`);
      if (el) { el.scrollIntoView({ block: "center", behavior: "smooth" }); el.classList.add("flash"); }
    });
  } else {
    window.scrollTo(0, 0);
  }
}

// ---------- Aktionen ----------
async function patchItem(id, body) {
  const updated = await api(`/api/items/${id}`, { method: "POST", body });
  for (const listName of ["deadlines", "feed", "notes", "events"]) {
    const arr = state.data[listName];
    const i = arr.findIndex((x) => x.id === id);
    if (i >= 0) arr[i] = updated;
  }
  state.data.unread = state.data.feed.filter((x) => !x.seen).length;
  render();
  return updated;
}

document.addEventListener("click", async (ev) => {
  const li = ev.target.closest("li.item");
  const act = ev.target.closest("[data-act]")?.dataset.act;
  if (li && act) {
    const id = Number(li.dataset.id);
    const it = findItem(id);
    if (!it) return;
    if (act === "toggle") {
      await patchItem(id, { done: !it.done }).catch((e) => toast(e.message));
      if (!it.done) toast("Erledigt ✓");
      return;
    }
    if (act === "open") {
      if (it.source === "manual") return openTaskDialog(it);
      if (!it.seen) patchItem(id, { seen: true }).catch(() => {});
      if (it.url) window.open(it.url, "_blank", "noopener");
      return;
    }
  }
  const sync = ev.target.closest("[data-sync]");
  if (sync) {
    sync.disabled = true;
    try {
      const r = await api(`/api/sync/${sync.dataset.sync}`, { method: "POST" });
      toast(r.status === "busy" ? "Läuft bereits …" : "Abruf gestartet …");
      pollSyncs();
    } catch (e) { toast(e.message); }
    return;
  }
  if (ev.target.closest("[data-klips]")) {
    try {
      await api("/api/klips/start", { method: "POST" });
      state.data.klips = { state: "starting", message: "Starte …" };
      render();
      startKlipsPoll();
    } catch (e) { toast(e.message); }
  }
});

let syncPollTimer = null;
function pollSyncs() {
  $("#refresh").classList.add("spin");
  clearInterval(syncPollTimer);
  let rounds = 0;
  syncPollTimer = setInterval(async () => {
    rounds++;
    try {
      const s = await api("/api/syncs");
      if (!s.running.filter((x) => x !== "klips").length || rounds > 150) {
        clearInterval(syncPollTimer);
        $("#refresh").classList.remove("spin");
        await load();
        const ilias = s.last.find((x) => x.source === "ilias");
        if (ilias && ilias.status === "error") toast("ILIAS: " + ilias.message.split("\n")[0], 5000);
        else toast("Aktualisiert");
      }
    } catch { clearInterval(syncPollTimer); $("#refresh").classList.remove("spin"); }
  }, 2000);
}

function startKlipsPoll() {
  clearInterval(state.klipsPoll);
  state.klipsPoll = setInterval(async () => {
    try {
      const k = await api("/api/klips/status");
      state.data.klips = k;
      render();
      if (!["starting", "login", "duo_wait", "syncing"].includes(k.state)) {
        clearInterval(state.klipsPoll);
        if (k.state === "done") { toast(k.message); load(); }
        if (k.state === "error") toast("KLIPS: " + k.message, 5000);
      }
    } catch { clearInterval(state.klipsPoll); }
  }, 1500);
}

$("#refresh").addEventListener("click", async () => {
  const d = state.data;
  try {
    if (d.sources.ilias) await api("/api/sync/ilias", { method: "POST" });
    if (d.sources.ical.length) await api("/api/sync/ical", { method: "POST" });
    toast("Abruf gestartet …");
    pollSyncs();
  } catch (e) { toast(e.message); }
});

$("#seen-all").addEventListener("click", async () => {
  await api("/api/seen-all", { method: "POST" });
  await load();
});

$("#feed-filter").addEventListener("click", (e) => {
  const b = e.target.closest(".chip"); if (!b) return;
  state.feedFilter = b.dataset.f;
  $$("#feed-filter .chip").forEach((c) => c.classList.toggle("active", c === b));
  renderFeed();
});
$("#dl-filter").addEventListener("click", (e) => {
  const b = e.target.closest(".chip"); if (!b) return;
  state.dlFilter = b.dataset.f;
  $$("#dl-filter .chip").forEach((c) => c.classList.toggle("active", c === b));
  renderDeadlines();
});
$("#show-done").addEventListener("change", (e) => { state.showDone = e.target.checked; renderDeadlines(); });

// ---------- Eigene Einträge ----------
function toLocalInput(iso) {
  const d = parseDate(iso);
  if (!d) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openTaskDialog(item = null) {
  state.editing = item;
  const f = $("#task-form");
  f.reset();
  $("#task-dialog-title").textContent = item ? "Eintrag bearbeiten" : "Neuer Eintrag";
  $("#task-delete").hidden = !item;
  if (item) {
    f.title.value = item.title;
    f.due_at.value = toLocalInput(item.due_at);
    f.course.value = item.course || "";
    f.detail.value = item.detail || "";
  }
  $("#task-dialog").showModal();
  setTimeout(() => f.title.focus(), 50);
}

$("#fab").addEventListener("click", () => openTaskDialog());
$("#task-cancel").addEventListener("click", () => $("#task-dialog").close());
$("#task-delete").addEventListener("click", async () => {
  if (!state.editing || !confirm("Eintrag löschen?")) return;
  await api(`/api/tasks/${state.editing.id}`, { method: "DELETE" });
  $("#task-dialog").close();
  await load();
});
$("#task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    title: f.title.value.trim(),
    due_at: f.due_at.value || null,
    course: f.course.value.trim(),
    detail: f.detail.value.trim(),
  };
  try {
    if (state.editing) await api(`/api/items/${state.editing.id}`, { method: "POST", body });
    else await api("/api/tasks", { method: "POST", body });
    $("#task-dialog").close();
    toast("Gespeichert");
    await load();
  } catch (err) { toast(err.message); }
});

// ---------- Push ----------
function urlB64ToUint8(b64) {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isStandalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone;

function pushSupported() {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

async function currentSub() {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function enablePush() {
  if (!pushSupported()) {
    toast(isIOS && !isStandalone ? "Erst über Teilen → „Zum Home-Bildschirm“ installieren" : "Push wird hier nicht unterstützt (HTTPS nötig)", 5000);
    return;
  }
  const perm = await Notification.requestPermission();
  if (perm !== "granted") { toast("Benachrichtigungen wurden nicht erlaubt"); return; }
  const { key } = await api("/api/push/key");
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToUint8(key) });
  await api("/api/push/subscribe", { method: "POST", body: sub.toJSON() });
  toast("Benachrichtigungen aktiv ✓");
  renderPushState();
}

async function renderPushState() {
  const sub = await currentSub().catch(() => null);
  const on = !!sub && Notification.permission === "granted";
  $("#push-banner").hidden = on;
  if (isIOS && !isStandalone) {
    $("#push-banner-text").textContent = "Auf dem iPhone zuerst über Teilen → „Zum Home-Bildschirm“ installieren, dann hier aktivieren.";
  }
  $("#push-state").textContent = !pushSupported()
    ? "Dieses Gerät/dieser Browser unterstützt hier keine Push-Nachrichten (HTTPS und Installation nötig)."
    : on ? "Aktiv auf diesem Gerät ✓" : "Auf diesem Gerät nicht aktiv.";
  $("#push-enable-2").hidden = on;
  $("#push-test").hidden = !on;
  $("#push-disable").hidden = !on;
  $("#ios-hint").hidden = !isIOS;
}

$("#push-enable").addEventListener("click", () => enablePush().catch((e) => toast(e.message)));
$("#push-enable-2").addEventListener("click", () => enablePush().catch((e) => toast(e.message)));
$("#push-test").addEventListener("click", async () => {
  const r = await api("/api/push/test", { method: "POST" });
  toast(r.sent ? "Test gesendet" : "Kein Gerät erreicht");
});
$("#push-disable").addEventListener("click", async () => {
  const sub = await currentSub();
  if (sub) {
    await api("/api/push/unsubscribe", { method: "POST", body: { endpoint: sub.endpoint } });
    await sub.unsubscribe();
  }
  renderPushState();
});

// ---------- Anmeldung ----------
function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-pw").focus();
}

async function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  await load();
  route();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-err").hidden = true;
  try {
    await api("/api/login", { method: "POST", body: { password: $("#login-pw").value } });
    $("#login-pw").value = "";
    showApp();
  } catch (err) {
    $("#login-err").textContent = err.message;
    $("#login-err").hidden = false;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  showLogin();
});

// ---------- Start ----------
window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("#app").hidden) load(); });

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("SW", e));
  navigator.serviceWorker.addEventListener("message", (e) => {
    if (e.data && e.data.type === "navigate") { location.hash = e.data.url.split("#")[1] || "home"; load(); }
  });
}

(async () => {
  try {
    const me = await fetch("/api/me", { credentials: "same-origin" }).then((r) => r.json());
    if (me.auth) showApp(); else showLogin();
  } catch {
    showLogin();
  }
  setInterval(() => { if (!document.hidden && !$("#app").hidden) load(); }, 5 * 60 * 1000);
})();
