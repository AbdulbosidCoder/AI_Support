// Read-only chats of AI Support for the support team's CRM.
// The key (an embed link "crmt_..." or a token "crm_...") comes in the URL fragment, which the browser
// never sends to a server; it is moved to this tab's sessionStorage and removed from the address bar.
"use strict";
(function () {
  const KEY = "ai_support_crm_key";
  let key = "";
  const m = location.hash.match(/key=([^&]+)/);
  if (m) {
    key = decodeURIComponent(m[1]);
    try { sessionStorage.setItem(KEY, key); } catch (e) { /* storage off: the key lives in memory only */ }
    history.replaceState(null, "", location.pathname);
  } else {
    try { key = sessionStorage.getItem(KEY) || ""; } catch (e) { key = ""; }
  }

  const $ = (id) => document.getElementById(id);
  const LABELS = { client: "Клиент", bot: "AI", operator: "Оператор", system: "" };
  let current = null;
  let scope = {};
  const blobs = [];

  function showError(text) { const el = $("error"); el.textContent = text; el.hidden = !text; }

  async function api(path) {
    const res = await fetch(path, { headers: { Authorization: "Bearer " + key }, cache: "no-store" });
    if (res.status === 401) throw new Error("Ссылка недействительна или устарела. Откройте чаты из CRM заново.");
    if (res.status === 403) throw new Error("Доступ с этого адреса запрещён.");
    if (res.status === 429) throw new Error("Слишком много запросов, попробуйте через минуту.");
    if (!res.ok) throw new Error("Ошибка сервера (" + res.status + ")");
    return res;
  }
  const json = async (path) => (await api(path)).json();

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  const when = (iso) => iso ? new Date(iso.replace(" ", "T") + (/[zZ+]/.test(iso.slice(10)) ? "" : "Z"))
    .toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "";

  async function loadList() {
    const status = $("status").value;
    const data = await json("/crm/v1/sessions?limit=200" + (status ? "&status=" + status : ""));
    const list = $("list");
    list.replaceChildren();
    if (!data.sessions.length) list.append(el("p", "muted center", "Сессий нет"));
    for (const s of data.sessions) {
      const row = el("button", "row" + (s.id === current ? " on" : ""));
      const title = el("div", "title");
      title.append(el("i", "dot" + (s.status === "open" ? " open" : "")), el("span", null, s.client_name));
      const who = s.operator_name || s.assigned_name || (s.escalated ? "ждёт оператора" : "AI");
      row.append(title, el("div", "muted", "#" + s.id + " · " + (s.topic || "без темы") + " · " + who),
                 el("div", "muted", when(s.opened_at)));
      row.dataset.id = s.id;
      row.onclick = () => openSession(s.id).catch((e) => showError(e.message));
      list.append(row);
    }
  }

  async function media(path, kind, into) {
    try {
      const blob = await (await api(path)).blob();
      const url = URL.createObjectURL(blob);
      blobs.push(url);
      const node = kind === "voice" || kind === "audio" || blob.type.startsWith("audio/") ? el("audio")
        : blob.type.startsWith("image/") ? el("img") : el("a", null, "Файл");
      if (node.tagName === "AUDIO") node.controls = true;
      if (node.tagName === "A") { node.href = url; node.download = ""; } else node.src = url;
      into.append(node);
    } catch (e) { into.append(el("div", "muted", "Файл недоступен")); }
  }

  async function openSession(id) {
    current = id;
    blobs.splice(0).forEach((u) => URL.revokeObjectURL(u));
    const s = await json("/crm/v1/sessions/" + id);
    const chat = $("chat");
    chat.replaceChildren();
    if (!scope.session_id) {
      const back = el("button", "back", "← Сессии");
      back.onclick = () => $("main").classList.remove("open");
      chat.append(back);
    }
    const meta = el("div", "meta");
    meta.append(el("b", null, s.client_name),
      el("div", "muted", ["Сессия #" + s.id, s.status === "open" ? "открыта" : "закрыта", s.topic,
        s.client_phone, s.language, s.rating ? "оценка " + s.rating.stars + "★" : ""].filter(Boolean).join(" · ")));
    chat.append(meta);
    for (const m of s.messages) {
      const b = el("div", "msg " + m.sender);
      const by = m.sender === "operator" ? (m.operator_name || LABELS.operator) : LABELS[m.sender];
      if (by && m.sender !== "client") b.append(el("div", "by", by));
      if (m.text) b.append(document.createTextNode(m.text));
      if (m.files && !m.media.length) b.append(el("div", "muted", "📎 вложение: " + m.files));
      if (m.media.length) {
        const files = el("div");
        b.append(files);
        for (const path of m.media) media(path, m.kind, files);
      }
      b.append(el("div", "at", when(m.created_at)));
      chat.append(b);
    }
    $("main").classList.add("open");
    document.querySelectorAll(".row").forEach((r) => r.classList.toggle("on", Number(r.dataset.id) === id));
  }

  async function refresh() {
    try {
      await loadList();
      showError("");
    } catch (e) { showError(e.message); }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    if (!key) { showError("Нет ключа доступа. Откройте чаты из CRM."); return; }
    try {
      scope = await json("/crm/v1/me");
      $("who").textContent = scope.name;
      $("status").onchange = refresh;
      if (scope.session_id) {
        $("list").hidden = true;
        $("main").style.gridTemplateColumns = "1fr";
        await openSession(scope.session_id);
      } else {
        await refresh();
        setInterval(refresh, 30000);
      }
    } catch (e) { showError(e.message); }
  });
})();
