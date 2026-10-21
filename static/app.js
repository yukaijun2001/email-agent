(() => {
  const state = {
    emails: [],
    accounts: [],
    accountErrors: {},   // account_id -> latest error message (list or watcher)
    selected: null,      // "<account_id>:<uid>"
    filter: "all",
    editing: null,       // account id being edited, "" for a new one, null when form hidden
    outlookProtocol: "outlook_imap",
    fresh: new Set(),    // keys of messages pushed over WebSocket, highlighted until opened
    animatedRows: new Set(),
    animatedMailboxes: new Set(),
  };
  document.documentElement.classList.add("motion-ready");
  const LIMIT = 50;
  const $ = (selector) => document.querySelector(selector);
  const list = $("#emailList");
  const listState = $("#listState");
  const mailboxList = $("#mailboxList");
  const accountCount = $("#accountCount");
  const mailboxHeading = $("#mailboxHeading");
  const search = $("#searchInput");
  const refresh = $("#refreshButton");
  const count = $("#resultCount");
  const connection = $("#connectionStatus");
  const live = $("#liveStatus");
  const accountName = $("#accountName");
  const accountErrors = $("#accountErrors");
  const banner = $("#newMailBanner");
  const help = $("#helpButton");
  const settings = $("#settingsButton");
  const dialog = $("#configDialog");
  const accountHome = $("#accountHome");
  const accountList = $("#accountList");
  const addAccount = $("#addAccountButton");
  const addOutlook = $("#addOutlookButton");
  const form = $("#configForm");
  const formTitle = $("#formTitle");
  const outlookForm = $("#outlookForm");
  const outlookTitle = $("#outlookTitle");
  const graphAccountInput = $("#graphAccount");
  const clientIdInput = $("#graphClientId");
  const refreshTokenInput = $("#graphRefreshToken");
  const outlookHint = $("#outlookHint");
  const outlookError = $("#outlookError");
  const outlookSave = $("#outlookSave");
  const outlookCancel = $("#outlookCancel");
  const outlookAuthorize = $("#outlookAuthorize");
  const outlookDevice = $("#outlookDevice");
  const outlookUserCode = $("#outlookUserCode");
  const outlookVerificationLink = $("#outlookVerificationLink");
  const outlookAuthStatus = $("#outlookAuthStatus");
  const outlookAdvanced = $("#outlookAdvanced");
  const serverInput = $("#imapServer");
  const portInput = $("#imapPort");
  const accountInput = $("#imapAccount");
  const passwordInput = $("#imapPassword");
  const passwordHint = $("#passwordHint");
  const configIntro = $("#configIntro");
  const configError = $("#configError");
  const configSave = $("#configSave");
  const configCancel = $("#configCancel");
  const configClose = $("#configClose");
  const helpDialog = $("#helpDialog");
  const helpClose = $("#helpClose");
  const messageDialog = $("#messageDialog");
  const messageDialogContent = $("#messageDialogContent");
  const messageClose = $("#messageClose");

  const EMPTY_DETAIL = '<div class="empty-detail"><span class="detail-mark" aria-hidden="true">/</span><h2>选择一封邮件</h2><p>点击右侧邮件卡片查看完整信息</p></div>';
  const keyOf = (email) => `${email.account_id}:${email.uid}`;
  const shortUid = (uid) => {
    const text = String(uid || "");
    return text.length > 12 ? `#${text.slice(-6)}` : `#${text}`;
  };
  const uidRank = (uid) => (/^[0-9]{1,15}$/.test(String(uid || "")) ? Number(uid) : 0);

  function updateHeaderOnScroll() {
    document.body.classList.toggle("scrolled", window.scrollY > 20);
  }

  updateHeaderOnScroll();
  window.addEventListener("scroll", updateHeaderOnScroll, { passive: true });

  // ------------------------------------------------------------------ helpers
  function formatDate(value) {
    if (!value) return "未知日期";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(parsed);
  }

  function senderName(value) {
    const match = String(value || "").match(/^\s*([^<]+?)(?:\s*<.*)?$/);
    return (match ? match[1] : value) || "未知发件人";
  }

  function accountById(id) {
    return state.accounts.find((item) => item.id === id);
  }

  function accountTitle(id) {
    const item = accountById(id);
    return item ? item.account : "已删除的邮箱";
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { cache: "no-store", ...options });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) {
      const error = new Error(payload.error || "请求失败。");
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function setStatus(message, error = false) {
    listState.hidden = false;
    listState.textContent = message;
    listState.className = error ? "list-state error" : "list-state";
    list.hidden = true;
  }

  // ------------------------------------------------------------------ accounts
  function applyAccounts(snapshot) {
    state.accounts = Array.isArray(snapshot.accounts) ? snapshot.accounts : [];
    const ids = new Set(state.accounts.map((item) => item.id));
    for (const id of Object.keys(state.accountErrors)) if (!ids.has(id)) delete state.accountErrors[id];
    accountCount.textContent = String(state.accounts.length);
    if (state.filter !== "all" && !ids.has(state.filter)) state.filter = "all";

    const configured = state.accounts.filter((item) => item.configured).length;
    accountName.textContent = !state.accounts.length
      ? "未配置"
      : state.accounts.length === 1 ? accountTitle(state.accounts[0].id) : `${state.accounts.length} 个邮箱`;
    connection.className = `connection${configured ? " ready" : " error"}`;
    connection.lastElementChild.textContent = configured ? "已连接配置" : "需要设置";
    renderAccountErrors();
    renderAccountList();
    renderMailboxList();
  }

  function renderAccountErrors() {
    const entries = Object.entries(state.accountErrors).filter(([id]) => accountById(id));
    accountErrors.hidden = !entries.length;
    accountErrors.replaceChildren(...entries.map(([id, message]) => {
      const line = document.createElement("p");
      const name = document.createElement("strong");
      name.textContent = accountTitle(id);
      line.append(name, `：${message}`);
      return line;
    }));
  }

  function renderAccountList() {
    if (!state.accounts.length) {
      const empty = document.createElement("li");
      empty.className = "account-empty";
      empty.textContent = "还没有添加邮箱。";
      accountList.replaceChildren(empty);
      return;
    }
    accountList.replaceChildren(...state.accounts.map((item) => {
      const row = document.createElement("li");
      row.className = "account-item";
      row.innerHTML = '<div class="account-info"><strong></strong><span></span></div><div class="account-actions"><button type="button" class="button button-quiet" data-action="edit">编辑</button><button type="button" class="button button-quiet danger" data-action="delete">删除</button></div>';
      row.querySelector("strong").textContent = item.account;
      row.querySelector("span").textContent = item.protocol === "graph"
        ? "Outlook · Graph"
        : item.protocol === "outlook_imap"
          ? "Outlook · IMAP OAuth2"
          : `${item.imap_server}:${item.imap_port}`;
      row.querySelector('[data-action="edit"]').addEventListener("click", () => (
        ["graph", "outlook_imap"].includes(item.protocol) ? showOutlook(item) : showForm(item)
      ));
      row.querySelector('[data-action="delete"]').addEventListener("click", () => deleteAccount(item));
      return row;
    }));
  }

  function mailboxEmailCount(id) {
    return id === "all" ? state.emails.length : state.emails.filter((email) => email.account_id === id).length;
  }

  function renderMailboxList() {
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const rows = state.accounts.map((item, index) => {
      const row = document.createElement("button");
      row.type = "button";
      const entering = !reduceMotion && !state.animatedMailboxes.has(item.id);
      row.className = `mailbox-row${state.filter === item.id ? " selected" : ""}${entering ? " row-enter" : ""}`;
      if (entering) row.style.setProperty("--row-delay", `${Math.min(index, 4) * 80}ms`);
      state.animatedMailboxes.add(item.id);
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(state.filter === item.id));
      row.innerHTML = '<span class="mailbox-avatar" aria-hidden="true"></span><span class="mailbox-copy"><strong></strong><small></small></span><span class="mailbox-unread"></span>';
      row.querySelector(".mailbox-avatar").textContent = (item.account || "?").slice(0, 1).toUpperCase();
      row.querySelector("strong").textContent = item.account;
      row.querySelector("small").textContent = item.configured ? "已连接 · INBOX" : "需要设置";
      row.querySelector(".mailbox-unread").textContent = mailboxEmailCount(item.id);
      row.addEventListener("click", () => { state.filter = item.id; renderMailboxList(); renderList(); });
      return row;
    });
    mailboxList.replaceChildren(...rows);
  }

  async function loadAccounts() {
    applyAccounts(await api("/api/accounts"));
  }

  // ------------------------------------------------------------------ list + detail
  function visibleEmails() {
    const query = search.value.trim().toLowerCase();
    return state.emails.filter((email) => {
      if (state.filter !== "all" && email.account_id !== state.filter) return false;
      return !query || `${email.from} ${email.subject}`.toLowerCase().includes(query);
    });
  }

  function renderList() {
    const emails = visibleEmails();
    count.textContent = `${emails.length} / ${state.emails.length} 封邮件`;
    mailboxHeading.textContent = state.filter === "all" ? "全部邮件" : accountTitle(state.filter);
    renderMailboxList();
    if (!emails.length) {
      setStatus(state.emails.length ? "没有匹配的邮件。" : "收件箱为空。", false);
      return;
    }
    listState.hidden = true;
    list.hidden = false;
    list.replaceChildren(...emails.map((email, index) => {
      const key = keyOf(email);
      const row = document.createElement("button");
      row.type = "button";
      const entering = !window.matchMedia("(prefers-reduced-motion: reduce)").matches && !state.animatedRows.has(key);
      row.className = `email-row${state.selected === key ? " selected" : ""}${state.fresh.has(key) ? " fresh" : ""}${entering ? " row-enter" : ""}`;
      row.style.setProperty("--row-delay", `${Math.min(index, 4) * 80}ms`);
      state.animatedRows.add(key);
      row.innerHTML = '<span class="row-top"><span class="sender"></span><span class="date"></span></span><span class="subject"></span><span class="preview"></span><span class="row-meta"><span class="account-tag"></span><span class="uid"></span></span>';
      row.querySelector(".sender").textContent = senderName(email.from);
      row.querySelector(".date").textContent = formatDate(email.date);
      row.querySelector(".subject").textContent = email.subject || "无主题";
      row.querySelector(".preview").textContent = email.from || "来自未知发件人的邮件";
      const tag = row.querySelector(".account-tag");
      tag.textContent = accountTitle(email.account_id);
      row.querySelector(".uid").textContent = shortUid(email.uid);
      row.addEventListener("click", () => openMessage(email));
      return row;
    }));
  }

  function showDetailLoading() {
    messageDialogContent.innerHTML = '<div class="empty-detail"><span class="detail-loading">正在读取邮件正文…</span></div>';
  }

  function messagePath(message) {
    return `/api/accounts/${encodeURIComponent(message.account_id)}/emails/${encodeURIComponent(message.uid)}`;
  }

  // The message is served as its own document (scripts removed, CSP
  // script-src 'none') and shown in a sandboxed iframe. Everything the mail
  // references is loaded. allow-same-origin without allow-scripts only lets
  // this page measure the content height; links open in a new tab.
  function mountMailFrame(container, message) {
    const frame = document.createElement("iframe");
    frame.className = "mail-frame";
    frame.title = `邮件正文：${message.subject || "无主题"}`;
    frame.setAttribute("sandbox", "allow-same-origin allow-popups allow-popups-to-escape-sandbox");
    frame.src = `${messagePath(message)}/html`;
    let observer = null;
    const fit = () => {
      try {
        const doc = frame.contentDocument;
        if (!doc || !doc.documentElement) return;
        const height = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0);
        frame.style.height = `${Math.max(height, 120)}px`;
      } catch (_) { /* keep the current height */ }
    };
    frame.addEventListener("load", () => {
      fit();
      try {
        const doc = frame.contentDocument;
        if (observer) observer.disconnect();
        if (window.ResizeObserver && doc && doc.documentElement) {
          observer = new ResizeObserver(fit);
          observer.observe(doc.documentElement);
        }
        // Remote images and web fonts finish after "load" in some cases.
        if (doc) for (const img of doc.images) img.addEventListener("load", fit);
        setTimeout(fit, 500);
        setTimeout(fit, 2000);
      } catch (_) { /* ignore */ }
    });
    container.replaceChildren(frame);
  }

  function renderDetail(message) {
    messageDialogContent.innerHTML = "";
    const wrapper = document.createElement("div");
    wrapper.className = "detail-content message-detail-content";
    wrapper.innerHTML = '<header class="detail-heading"><span class="eyebrow">MESSAGE / READ ONLY</span><h2 id="messageDialogTitle"></h2><div class="detail-meta"><div class="meta-row"><span class="meta-label">发件人</span><span class="meta-value from"></span></div><div class="meta-row"><span class="meta-label">时间</span><span class="meta-value date"></span></div><div class="meta-row"><span class="meta-label">邮箱</span><span class="meta-value mailbox"></span></div><div class="meta-row"><span class="meta-label">编号</span><span class="meta-value uid"></span></div></div><div class="detail-recipients"></div></header>'
      + '<div class="detail-body rich"></div>';
    wrapper.querySelector("h2").textContent = message.subject || "无主题";
    wrapper.querySelector(".from").textContent = message.from || "未知发件人";
    wrapper.querySelector(".date").textContent = message.date || "未知日期";
    wrapper.querySelector(".mailbox").textContent = accountTitle(message.account_id);
    wrapper.querySelector(".uid").textContent = shortUid(message.uid);
    const recipients = wrapper.querySelector(".detail-recipients");
    for (const [label, value] of [["收件人", message.to], ["抄送", message.cc]]) {
      if (!value) continue;
      const line = document.createElement("div");
      const name = document.createElement("span");
      name.textContent = `${label}：`;
      line.append(name, value);
      recipients.appendChild(line);
    }
    recipients.hidden = !recipients.childElementCount;
    messageDialogContent.appendChild(wrapper);
    mountMailFrame(wrapper.querySelector(".detail-body"), message);
  }

  async function openMessage(email) {
    const key = keyOf(email);
    state.selected = key;
    state.fresh.delete(key);
    renderList();
    showDetailLoading();
    if (typeof messageDialog.showModal === "function") {
      if (!messageDialog.open) messageDialog.showModal();
    } else messageDialog.setAttribute("open", "");
    try {
      const payload = await api(messagePath(email));
      if (state.selected === key) renderDetail(payload);
    } catch (error) {
      if (state.selected !== key) return;
      messageDialogContent.innerHTML = '<div class="empty-detail"><span class="detail-mark" aria-hidden="true">!</span><h2>无法读取</h2><p></p></div>';
      messageDialogContent.querySelector("p").textContent = error.message;
    }
  }

  function sortEmails(emails) {
    return emails
      .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0) || uidRank(b.uid) - uidRank(a.uid))
      .slice(0, LIMIT);
  }

  let loadGeneration = 0;

  // Each mailbox is requested separately so fast mailboxes appear immediately
  // instead of waiting for the slowest one.
  async function loadEmails() {
    const generation = ++loadGeneration;
    if (!state.accounts.length) {
      count.textContent = "尚未连接";
      state.emails = [];
      setStatus("请先在右上角“邮箱管理”中添加邮箱。", false);
      return;
    }
    refresh.disabled = true;
    refresh.classList.add("loading");
    setStatus("正在同步收件箱…");
    state.emails = [];
    state.animatedRows.clear();
    state.accountErrors = {};
    state.selected = null;
    state.fresh.clear();
    hideBanner();
    renderAccountErrors();
    messageDialogContent.innerHTML = '<div class="empty-detail"><span class="detail-mark" aria-hidden="true">/</span><h2>选择一封邮件</h2><p>点击右侧邮件卡片查看完整信息</p></div>';

    const accounts = state.accounts.slice();
    let pending = accounts.length;
    let succeeded = 0;
    const progress = () => {
      if (pending) count.textContent = `${visibleEmails().length} 封邮件 · 还有 ${pending} 个邮箱同步中`;
    };
    progress();

    await Promise.all(accounts.map(async (item) => {
      try {
        const payload = await api(`/api/emails?account=${encodeURIComponent(item.id)}&limit=${LIMIT}`);
        if (generation !== loadGeneration) return;
        const incoming = Array.isArray(payload.emails) ? payload.emails : [];
        const known = new Set(state.emails.map(keyOf));
        state.emails = sortEmails([...state.emails, ...incoming.filter((email) => !known.has(keyOf(email)))]);
        succeeded += 1;
      } catch (error) {
        if (generation !== loadGeneration) return;
        state.accountErrors[item.id] = error.message;
        renderAccountErrors();
      } finally {
        if (generation === loadGeneration) {
          pending -= 1;
          if (state.emails.length || !pending) renderList();
          progress();
        }
      }
    }));

    if (generation !== loadGeneration) return;
    refresh.disabled = false;
    refresh.classList.remove("loading");
    if (!succeeded) {
      setStatus(accounts.length === 1 ? state.accountErrors[accounts[0].id] || "读取收件箱失败。" : "所有邮箱均读取失败，请检查连接设置。", true);
      count.textContent = "同步失败";
    } else if (!state.emails.length) {
      listState.hidden = false;
      listState.textContent = "收件箱为空。";
    }
  }

  // ------------------------------------------------------------------ realtime
  let socket = null;
  let retryDelay = 1000;
  let bannerTimer = null;

  function setLive(status) {
    const labels = { open: "实时推送已连接", connecting: "实时推送连接中", closed: "实时推送已断开，重连中" };
    live.className = `connection${status === "open" ? " ready" : status === "closed" ? " error" : ""}`;
    live.lastElementChild.textContent = labels[status];
  }

  function hideBanner() {
    banner.hidden = true;
    clearTimeout(bannerTimer);
  }

  function showBanner(text) {
    banner.textContent = text;
    banner.hidden = false;
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(hideBanner, 8000);
  }

  function handleNewEmails(event) {
    const known = new Set(state.emails.map(keyOf));
    const incoming = (event.emails || []).filter((email) => !known.has(keyOf(email)));
    if (!incoming.length) return;
    for (const email of incoming) state.fresh.add(keyOf(email));
    state.emails = sortEmails([...incoming, ...state.emails]);
    renderList();
    const name = accountTitle(event.account_id);
    const first = incoming[0];
    showBanner(incoming.length === 1
      ? `新邮件 · ${name}：${senderName(first.from)} — ${first.subject || "无主题"}`
      : `${name} 收到 ${incoming.length} 封新邮件`);
    if (document.hidden) document.title = `(${state.fresh.size}) 收件箱 · 只读邮件`;
  }

  function handleEvent(event) {
    if (event.type === "new_emails") {
      handleNewEmails(event);
    } else if (event.type === "account_status") {
      if (event.ok) delete state.accountErrors[event.account_id];
      else state.accountErrors[event.account_id] = event.error;
      renderAccountErrors();
    } else if (event.type === "accounts_changed") {
      // Another tab (or this one) edited the account list.
      loadAccounts().catch(() => {});
    }
  }

  function connectSocket() {
    setLive("connecting");
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.addEventListener("open", () => { retryDelay = 1000; setLive("open"); });
    socket.addEventListener("message", (message) => {
      try { handleEvent(JSON.parse(message.data)); } catch (_) { /* ignore malformed */ }
    });
    socket.addEventListener("close", () => {
      setLive("closed");
      setTimeout(connectSocket, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    });
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) document.title = "收件箱 · 只读邮件";
  });

  // ------------------------------------------------------------------ dialogs
  function showForm(item) {
    state.editing = item ? item.id : "";
    outlookForm.hidden = true;
    accountHome.hidden = true;
    configError.hidden = true;
    form.hidden = false;
    form.classList.remove("form-enter");
    requestAnimationFrame(() => form.classList.add("form-enter"));
    formTitle.textContent = item ? `编辑：${item.account}` : "添加 IMAP 邮箱";
    serverInput.value = item ? item.imap_server : "";
    portInput.value = item ? item.imap_port : 993;
    accountInput.value = item ? item.account : "";
    passwordInput.value = "";
    passwordInput.required = !(item && item.has_password);
    passwordHint.textContent = item && item.has_password
      ? "留空即可保留原授权码。"
      : "授权码保存在部署服务器，用于连接 IMAP，不会通过接口返回。";
    serverInput.focus();
  }

  let outlookPollTimer = null;
  let outlookFlowId = "";

  function resetOutlookFlow() {
    window.clearTimeout(outlookPollTimer);
    outlookPollTimer = null;
    outlookFlowId = "";
    outlookDevice.hidden = true;
    outlookUserCode.textContent = "";
    outlookAuthStatus.textContent = "等待授权…";
    outlookAuthorize.disabled = false;
    outlookAuthorize.textContent = "开始 Microsoft 授权";
  }

  function hideForm() {
    resetOutlookFlow();
    state.editing = null;
    form.hidden = true;
    outlookForm.hidden = true;
    accountHome.hidden = false;
    configError.hidden = true;
    outlookError.hidden = true;
  }

  function showOutlook(item) {
    resetOutlookFlow();
    state.editing = item ? item.id : "";
    state.outlookProtocol = item && item.protocol === "graph" ? "graph" : "outlook_imap";
    form.hidden = true;
    accountHome.hidden = true;
    outlookError.hidden = true;
    outlookForm.hidden = false;
    outlookForm.classList.remove("form-enter");
    requestAnimationFrame(() => outlookForm.classList.add("form-enter"));
    outlookTitle.textContent = item
      ? `编辑 Outlook：${item.account}`
      : "添加 Outlook IMAP";
    graphAccountInput.value = item ? item.account : "";
    clientIdInput.value = item ? item.client_id || "" : "";
    refreshTokenInput.value = "";
    refreshTokenInput.required = false;
    const legacyGraph = state.outlookProtocol === "graph";
    outlookAuthorize.hidden = legacyGraph;
    outlookAdvanced.open = legacyGraph;
    outlookHint.textContent = legacyGraph
      ? "这是已有的 Graph 账号；可留空 refresh token 保留原值。新 Outlook 账号默认使用 IMAP OAuth2。"
      : item
        ? "点击开始授权可更换 OAuth2 凭据；已有 refresh token 也可以在高级区域直接保存。"
        : "应用需允许个人 Microsoft 账号并启用公共客户端流；无需填写邮箱密码。";
    outlookSave.textContent = legacyGraph ? "保存 Graph 设置" : "保存已有 Token";
    graphAccountInput.focus();
  }

  let configCloseTimer = null;
  let helpCloseTimer = null;

  async function openConfig() {
    try { await loadAccounts(); } catch (_) { /* keep last known list */ }
    configIntro.textContent = state.accounts.length
      ? "可以添加多个邮箱，收件箱会合并显示所有邮箱的邮件。"
      : "添加 IMAP 邮箱，或单独添加 Outlook。";
    hideForm();
    clearTimeout(configCloseTimer);
    dialog.classList.remove("is-closing");
    if (typeof dialog.showModal === "function") { if (!dialog.open) dialog.showModal(); }
    else dialog.setAttribute("open", "");
  }

  function closeConfig() {
    if (!dialog.open && !dialog.hasAttribute("open")) return;
    hideForm();
    dialog.classList.add("is-closing");
    clearTimeout(configCloseTimer);
    configCloseTimer = window.setTimeout(() => {
      if (!dialog.classList.contains("is-closing")) return;
      dialog.classList.remove("is-closing");
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }, 160);
  }

  function openHelp() {
    clearTimeout(helpCloseTimer);
    helpDialog.classList.remove("is-closing");
    if (typeof helpDialog.showModal === "function") helpDialog.showModal();
    else helpDialog.setAttribute("open", "");
  }

  function closeHelp() {
    if (!helpDialog.open && !helpDialog.hasAttribute("open")) return;
    helpDialog.classList.add("is-closing");
    clearTimeout(helpCloseTimer);
    helpCloseTimer = window.setTimeout(() => {
      if (!helpDialog.classList.contains("is-closing")) return;
      helpDialog.classList.remove("is-closing");
      if (typeof helpDialog.close === "function") helpDialog.close();
      else helpDialog.removeAttribute("open");
    }, 160);
  }

  function closeMessage() {
    if (typeof messageDialog.close === "function") messageDialog.close();
    else messageDialog.removeAttribute("open");
  }

  async function saveConfig(event) {
    event.preventDefault();
    configError.hidden = true;
    configSave.disabled = true;
    configSave.textContent = "保存中…";
    const editing = state.editing;
    try {
      const body = JSON.stringify({
        protocol: "imap",
        imap_server: serverInput.value.trim(),
        imap_port: Number(portInput.value),
        account: accountInput.value.trim(),
        password: passwordInput.value,
      });
      const path = editing ? `/api/accounts/${encodeURIComponent(editing)}` : "/api/accounts";
      await api(path, { method: editing ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body });
      await loadAccounts();
      hideForm();
      await loadEmails();
    } catch (error) {
      configError.hidden = false;
      configError.textContent = error.message;
    } finally {
      configSave.disabled = false;
      configSave.textContent = "保存并连接";
    }
  }

  async function deleteAccount(item) {
    if (!window.confirm(`删除邮箱“${item.account}”？服务器保存的授权码也会一并删除。`)) return;
    try {
      applyAccounts(await api(`/api/accounts/${encodeURIComponent(item.id)}`, { method: "DELETE" }));
      if (state.editing === item.id) hideForm();
      state.emails = state.emails.filter((email) => email.account_id !== item.id);
      if (state.selected && state.selected.startsWith(`${item.id}:`)) {
        state.selected = null;
        messageDialogContent.innerHTML = '<div class="empty-detail"><span class="detail-mark" aria-hidden="true">/</span><h2>选择一封邮件</h2><p>点击右侧邮件卡片查看完整信息</p></div>';
      }
      if (state.accounts.length) renderList(); else await loadEmails();
    } catch (error) {
      configError.hidden = false;
      configError.textContent = error.message;
    }
  }

  // ------------------------------------------------------------------ bootstrap
  async function bootstrap() {
    try {
      await loadAccounts();
      if (!state.accounts.length) {
        setStatus("请先添加邮箱。", false);
        await openConfig();
      } else {
        await loadEmails();
      }
    } catch (error) {
      connection.className = "connection error";
      connection.lastElementChild.textContent = "服务不可用";
      setStatus(error.message, true);
    }
    connectSocket();
  }

  search.addEventListener("input", renderList);
  refresh.addEventListener("click", () => { if (state.accounts.length) loadEmails(); else openConfig(); });
  banner.addEventListener("click", hideBanner);
  help.addEventListener("click", openHelp);
  helpClose.addEventListener("click", closeHelp);
  settings.addEventListener("click", openConfig);

  async function pollOutlookAuthorization(delaySeconds) {
    window.clearTimeout(outlookPollTimer);
    outlookPollTimer = window.setTimeout(async () => {
      if (!outlookFlowId || outlookForm.hidden) return;
      try {
        const result = await api("/api/outlook/device/poll", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ flow_id: outlookFlowId }),
        });
        if (result.status === "complete") {
          outlookAuthStatus.textContent = "授权完成，正在读取 INBOX…";
          outlookFlowId = "";
          await loadAccounts();
          hideForm();
          await loadEmails();
          return;
        }
        outlookAuthStatus.textContent = "等待你在 Microsoft 页面完成授权…";
        pollOutlookAuthorization(Number(result.retry_after) || 5);
      } catch (error) {
        outlookFlowId = "";
        outlookAuthorize.disabled = false;
        outlookAuthorize.textContent = "重新开始授权";
        outlookError.hidden = false;
        outlookError.textContent = error.message;
        outlookAuthStatus.textContent = "授权未完成。";
      }
    }, Math.max(1, Number(delaySeconds) || 5) * 1000);
  }

  async function startOutlookAuthorization() {
    outlookError.hidden = true;
    outlookAuthorize.disabled = true;
    outlookAuthorize.textContent = "正在创建授权…";
    try {
      const result = await api("/api/outlook/device/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account: graphAccountInput.value.trim(),
          client_id: clientIdInput.value.trim(),
          account_id: state.editing || "",
        }),
      });
      outlookFlowId = result.flow_id;
      outlookUserCode.textContent = result.user_code;
      const verification = new URL(result.verification_uri);
      if (verification.protocol !== "https:") throw new Error("Microsoft 返回了无效的授权地址。");
      outlookVerificationLink.href = verification.href;
      outlookDevice.hidden = false;
      outlookAuthStatus.textContent = "等待你在 Microsoft 页面完成授权…";
      outlookAuthorize.textContent = "授权进行中";
      pollOutlookAuthorization(Number(result.interval) || 5);
    } catch (error) {
      resetOutlookFlow();
      outlookError.hidden = false;
      outlookError.textContent = error.message;
    }
  }

  async function saveOutlook(event) {
    event.preventDefault();
    outlookError.hidden = true;
    outlookSave.disabled = true;
    outlookSave.textContent = "保存中…";
    const editing = state.editing;
    try {
      const body = JSON.stringify({
        protocol: state.outlookProtocol,
        account: graphAccountInput.value.trim(),
        password: "",
        client_id: clientIdInput.value.trim(),
        refresh_token: refreshTokenInput.value.trim(),
      });
      const path = editing ? `/api/accounts/${encodeURIComponent(editing)}` : "/api/accounts";
      await api(path, { method: editing ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body });
      await loadAccounts();
      hideForm();
      await loadEmails();
    } catch (error) {
      outlookError.hidden = false;
      outlookError.textContent = error.message;
    } finally {
      outlookSave.disabled = false;
      outlookSave.textContent = state.outlookProtocol === "graph" ? "保存 Graph 设置" : "保存已有 Token";
    }
  }

  addAccount.addEventListener("click", () => showForm(null));
  addOutlook.addEventListener("click", () => showOutlook(null));
  outlookCancel.addEventListener("click", hideForm);
  outlookForm.addEventListener("submit", saveOutlook);
  outlookAuthorize.addEventListener("click", startOutlookAuthorization);
  form.addEventListener("submit", saveConfig);
  configCancel.addEventListener("click", hideForm);
  configClose.addEventListener("click", closeConfig);
  dialog.addEventListener("cancel", (event) => {
    if (!form.hidden || !outlookForm.hidden) {
      event.preventDefault();
      hideForm();
      return;
    }
    event.preventDefault();
    closeConfig();
  });
  helpDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeHelp(); });
  messageClose.addEventListener("click", closeMessage);
  messageDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeMessage(); });
  bootstrap();
})();
