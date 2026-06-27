const lang = {
  current: 'en',
  cache: {},
};

async function loadLangFile(code) {
  if (lang.cache[code]) return lang.cache[code];
  try {
    const response = await fetch(`/dashboard/assets/lang/${code}.json`);
    if (!response.ok) throw new Error('Not found');
    const data = await response.json();
    lang.cache[code] = data;
    return data;
  } catch {
    lang.cache[code] = {};
    return {};
  }
}

function t(key) {
  const map = lang.cache[lang.current];
  const fallback = lang.cache.en;
  return (map && map[key]) || (fallback && fallback[key]) || key;
}

async function setLang(code) {
  const data = await loadLangFile(code);
  if (!data || Object.keys(data).length === 0) return;
  lang.current = code;
  document.documentElement.lang = code;
  applyTranslations();
  try { localStorage.setItem('eb-lang', code); } catch {}
  const btn = $('#setting-lang');
  if (btn) btn.value = code;
  if (state.lastData) render(state.lastData);
}

async function loadLang() {
  let code = 'en';
  try { code = localStorage.getItem('eb-lang') || 'en'; } catch {}
  await loadLangFile('en');
  await setLang(code);
}

function applyTranslations() {
  document.querySelectorAll('[data-i18n]').forEach((el) => {
    if (el.querySelector('[data-i18n]')) return;
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-title]').forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
}

const state = {
  timer: null,
  lastData: null,
};

const $ = (selector) => document.querySelector(selector);

function text(selector, value) {
  const node = $(selector);
  if (node) node.textContent = value;
}

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  window.clearTimeout(showToast.hideTimer);
  showToast.hideTimer = window.setTimeout(() => toast.classList.remove('show'), 2800);
}

function formatDate(ms) {
  if (!ms) return '-';
  try {
    const locale = lang.current === 'ko' ? 'ko-KR' : 'en-US';
    return new Intl.DateTimeFormat(locale, {
      month: '2-digit',
      day: '2-digit',
    }).format(new Date(ms));
  } catch {
    return String(ms);
  }
}

function formatLogTs(ms) {
  if (!ms) return '-';
  try {
    const locale = lang.current === 'ko' ? 'ko-KR' : 'en-US';
    return new Intl.DateTimeFormat(locale, {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    }).format(new Date(ms));
  } catch {
    return String(ms);
  }
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function emptyRow(columns, message) {
  return `<tr><td colspan="${columns}" class="muted">${escapeHtml(message)}</td></tr>`;
}

function setConnection(ok, label) {
  const dot = $('#status-dot');
  dot.classList.toggle('ok', ok);
  dot.classList.toggle('bad', !ok);
  text('#status-text', label);
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      const body = await response.json();
      throw new Error(body?.error?.message || `${response.status} ${response.statusText}`);
    }
    const body = await response.text();
    throw new Error(body || `${response.status} ${response.statusText}`);
  }
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) return response.json();
  return null;
}

async function loadDashboard(showSuccess = false) {
  try {
    const data = await requestJson('/api/dashboard');
    state.lastData = data;
    render(data);
    setConnection(true, t('status.online'));
    if (showSuccess) showToast(t('toast.refreshed'));
  } catch (error) {
    setConnection(false, t('status.offline'));
    showToast(`${t('toast.fetchFailed')}: ${error.message}`);
  }
}

function render(data) {
  const bridge = data.bridge || {};
  const server = data.server || {};
  const mqtt = bridge.mqtt || {};
  const redirects = data.redirects || [];
  const callbacks = data.callbacks || [];
  const registrations = data.registrations || [];
  const sessions = data.mqttSessions || [];
  const version = server.version || bridge.bridgeVersion || '';
  const isOnline = true;

  text('#bridge-version', `EdgeBridge ${version}`.trim());
  text('#bridge-meta', `포트 ${server.serverPort || '-'} · ${server.mdnsName || 'mDNS'}`);
  text('#server-version', `v${version}`.trim() || '-');
  text('#server-port', String(server.serverPort || '-'));

  const patOk = bridge.stTokenConfigured;
  text('#pat-state', patOk ? (bridge.stTokenValid ? 'Valid' : 'Configured') : 'Not set');
  const patDot = $('#pat-indicator');
  if (patDot) {
    patDot.classList.toggle('ok', !!patOk);
    patDot.classList.toggle('bad', !patOk);
  }

  text('#mqtt-count', String(mqtt.total ?? sessions.length));
  text('#mqtt-connected', String(mqtt.connected ?? sessions.filter((s) => s.state === 'CONNECTED').length));
  text('#redirect-count', String(redirects.length));
  text('#callback-count', String(callbacks.length));
  text('#registration-count', String(registrations.length));

  const mdnsOn = !!server.mdnsEnabled;
  text('#mdns-state', mdnsOn ? 'On' : 'Off');
  const mdnsDot = $('#mdns-indicator');
  if (mdnsDot) {
    mdnsDot.classList.toggle('ok', mdnsOn);
    mdnsDot.classList.toggle('bad', !mdnsOn);
  }

  text('#data-dir', server.dataDir || '-');

  renderMqtt(sessions);
  renderRedirects(redirects);
  renderCallbacks(callbacks);
  renderRegistrations(registrations);
}

function renderMqtt(sessions) {
  const tbody = $('#mqtt-table');
  if (!sessions.length) {
    tbody.innerHTML = emptyRow(5, t('mqtt.empty'));
    return;
  }
  tbody.innerHTML = sessions.map((session) => `
    <tr>
      <td><strong>${escapeHtml(session.id)}</strong><br><span class="muted">${escapeHtml(session.effectiveClientId || '-')}</span></td>
      <td><span class="pill">${escapeHtml(session.state || 'CREATED')}</span></td>
      <td>${escapeHtml((session.subscribedTopics || []).join(', ') || '-')}</td>
      <td>${escapeHtml(session.forwardTarget || '-')}<br><span class="muted">pending ${escapeHtml(session.pendingForwardCount || 0)}</span></td>
      <td>${escapeHtml(session.lastError || '-')}</td>
    </tr>
  `).join('');
}

const logState = {
  entries: [],
  autoScroll: true,
};

function levelClass(level) {
  if (level === 'error') return 'log-error';
  if (level === 'warn') return 'log-warn';
  if (level === 'hilite') return 'log-hilite';
  if (level === 'debug') return 'log-debug';
  return 'log-info';
}

async function loadLogs() {
  try {
    const data = await requestJson('/api/logs');
    const logs = data.logs || [];
    text('#log-count', String(logs.length));
    const viewer = $('#log-viewer');
    const prevLen = logState.entries.length;
    const atBottom = viewer.scrollHeight - viewer.scrollTop - viewer.clientHeight < 40;
    logState.entries = logs;
    const firstNew = prevLen === 0 ? 0 : prevLen;
    const fragment = document.createDocumentFragment();
    for (let i = firstNew; i < logs.length; i++) {
      const e = logs[i];
      const div = document.createElement('div');
      div.className = `log-line ${levelClass(e.level)}`;
      div.innerHTML = `<span class="log-ts">${escapeHtml(formatLogTs(e.ts))}</span> <span class="log-level">${escapeHtml(e.level)}</span> <span class="log-msg">${escapeHtml(e.msg)}</span>`;
      fragment.appendChild(div);
    }
    if (firstNew === 0) {
      viewer.innerHTML = '';
    }
    viewer.appendChild(fragment);
    if (atBottom || firstNew === 0) {
      viewer.scrollTop = viewer.scrollHeight;
    }
  } catch {
    // silent
  }
}

function clearLogs() {
  logState.entries = [];
  $('#log-viewer').innerHTML = '';
  text('#log-count', '0');
}

function renderRedirects(redirects) {
  const list = $('#redirect-list');
  if (!redirects.length) {
    list.innerHTML = `<div class="empty">${t('redirects.empty')}</div>`;
    return;
  }
  list.innerHTML = redirects.map((item) => `
    <div class="list-item">
      <div>
        <strong>${escapeHtml(item.path)}</strong>
        <code>${escapeHtml(item.targetBase)}</code>
      </div>
      <button class="icon-button danger" type="button" data-delete-redirect="${escapeHtml(item.path)}" title="${t('redirects.delete')}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>
        <span>Delete</span>
      </button>
    </div>
  `).join('');
}

function renderCallbacks(callbacks) {
  const list = $('#callback-list');
  if (!callbacks.length) {
    list.innerHTML = `<div class="empty">${t('callbacks.empty')}</div>`;
    return;
  }
  list.innerHTML = callbacks.map((item) => `
    <div class="list-item">
      <div>
        <strong>${escapeHtml(item.name)}</strong>
        <code>${escapeHtml(trimValue(item.value))}</code>
      </div>
      <button class="icon-button danger" type="button" data-delete-callback="${escapeHtml(item.name)}" title="${t('callbacks.delete')}">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>
        <span>Delete</span>
      </button>
    </div>
  `).join('');
}

function trimValue(value) {
  const textValue = String(value ?? '');
  return textValue.length > 140 ? `${textValue.slice(0, 140)}...` : textValue || '(empty)';
}

function renderRegistrations(registrations) {
  const tbody = $('#registration-table');
  if (!registrations.length) {
    tbody.innerHTML = emptyRow(3, t('registrations.empty'));
    return;
  }
  tbody.innerHTML = registrations.map((item) => `
    <tr>
      <td>${escapeHtml(item.devaddr || '-')}</td>
      <td>${escapeHtml(item.hubaddr || '-')}</td>
      <td>${escapeHtml(item.edgeid || '-')}</td>
    </tr>
  `).join('');
}

async function addRedirect(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const params = new URLSearchParams(new FormData(form));
  try {
    await fetch(`/api/redirect?${params.toString()}`, { method: 'POST' }).then((response) => {
      if (!response.ok) return response.text().then((body) => Promise.reject(new Error(body || t('toast.redirectFailed'))));
      return null;
    });
    form.reset();
    showToast(t('toast.redirectAdded'));
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
}

async function addCallback(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const formData = new FormData(form);
  const name = formData.get('name');
  const value = formData.get('value') || '';
  try {
    await fetch(`/api/callback?name=${encodeURIComponent(name)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      body: value,
    }).then((response) => {
      if (!response.ok) return response.text().then((body) => Promise.reject(new Error(body || t('toast.callbackFailed'))));
      return null;
    });
    form.reset();
    showToast(t('toast.callbackSaved'));
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
}

async function handleListClick(event) {
  const redirectPath = event.target.closest('[data-delete-redirect]')?.dataset.deleteRedirect;
  const callbackName = event.target.closest('[data-delete-callback]')?.dataset.deleteCallback;
  if (!redirectPath && !callbackName) return;

  try {
    if (redirectPath) {
      await fetch(`/api/redirect?path=${encodeURIComponent(redirectPath)}`, { method: 'DELETE' }).then((response) => {
        if (!response.ok) throw new Error(t('toast.redirectFailed'));
      });
      showToast(t('toast.redirectDeleted'));
    }
    if (callbackName) {
      await fetch(`/api/callback?name=${encodeURIComponent(callbackName)}`, { method: 'DELETE' }).then((response) => {
        if (!response.ok) throw new Error(t('toast.callbackFailed'));
      });
      showToast(t('toast.callbackDeleted'));
    }
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
}

function switchPage(pageName) {
  document.querySelectorAll('.page').forEach((p) => {
    p.classList.toggle('active', p.dataset.page === pageName);
  });
  const selector = '.nav a, .side-settings';
  document.querySelectorAll(selector).forEach((link) => {
    const hash = '#' + pageName;
    link.classList.toggle('active', link.getAttribute('href') === hash);
  });
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function applySettingsToForm(settings) {
  $('#setting-token').value = settings.stToken || '';
  $('#setting-fw').value = settings.forwardingTimeout ?? '';
  $('#setting-mdns-name').value = settings.mdnsName || '';
  setSwitchState(!!settings.mdnsEnabled);
  const note = $('#settings-note');
  const envOverrides = settings.source?.envOverrides || {};
  const overrideCount = Object.values(envOverrides).filter(Boolean).length;
  note.textContent = overrideCount
    ? `${overrideCount} ${t('settings.envOverrides')}`
    : t('settings.noEnvOverrides');
}

function setSwitchState(on) {
  const button = $('#setting-mdns-enabled');
  button.dataset.on = on ? 'true' : 'false';
  button.setAttribute('aria-checked', on ? 'true' : 'false');
  button.classList.toggle('is-on', on);
}

function setTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  const btn = $('#setting-theme');
  if (btn) {
    btn.dataset.on = dark ? 'true' : 'false';
    btn.setAttribute('aria-checked', dark ? 'true' : 'false');
    btn.classList.toggle('is-on', dark);
  }
  try { localStorage.setItem('eb-theme', dark ? 'dark' : 'light'); } catch {}
}

function loadTheme() {
  let dark = false;
  try { dark = localStorage.getItem('eb-theme') === 'dark'; } catch {}
  setTheme(dark);
}

async function loadSettings() {
  try {
    const settings = await requestJson('/api/settings');
    applySettingsToForm(settings);
  } catch (error) {
    showToast(`${t('toast.settingsFailed')}: ${error.message}`);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const payload = {
    stToken: $('#setting-token').value.trim(),
    forwardingTimeout: Number($('#setting-fw').value),
    mdnsName: $('#setting-mdns-name').value.trim(),
    mdnsEnabled: $('#setting-mdns-enabled').dataset.on === 'true',
  };
  try {
    const result = await requestJson('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    applySettingsToForm(result.settings || payload);
    showToast(t('toast.saved'));
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
}

function handleNav() {
  const pageName = (location.hash || '#overview').slice(1);
  switchPage(pageName);
}

async function init() {
  await loadLang();
  loadTheme();
  handleNav();
  window.addEventListener('hashchange', handleNav);
  $('#refresh-button').addEventListener('click', () => loadDashboard(true));
  $('#settings-form').addEventListener('submit', saveSettings);
  $('#log-clear').addEventListener('click', clearLogs);
  $('#setting-mdns-enabled').addEventListener('click', () => {
    const button = $('#setting-mdns-enabled');
    setSwitchState(button.dataset.on !== 'true');
  });
  $('#setting-theme').addEventListener('click', () => {
    const btn = $('#setting-theme');
    setTheme(btn.dataset.on !== 'true');
  });
  $('#setting-lang').addEventListener('change', (e) => setLang(e.target.value));
  $('#redirect-form').addEventListener('submit', addRedirect);
  $('#callback-form').addEventListener('submit', addCallback);
  document.addEventListener('click', handleListClick);
  loadDashboard();
  loadSettings();
  loadLogs();
  state.timer = window.setInterval(loadDashboard, 15000);
  state.logTimer = window.setInterval(loadLogs, 3000);
}

window.addEventListener('beforeunload', () => {
  window.clearInterval(state.timer);
  window.clearInterval(state.logTimer);
});
init();
