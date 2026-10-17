'use strict';

/* 管理台前端：哈希路由 + 原生 fetch，无框架、无构建、无第三方依赖。
   接口契约来自 wxbot_framework/admin/api.py，静态资源规则来自 server.py：
   - 管理台自己的页面挂在 /（就是本目录）；
   - 插件自带的页面挂在 /plugin/<插件名>/<插件内路径>，数据在 /api/plugin/<插件名>/<动作>。 */

const LOG_PAGE_SIZE = 50;      // 日志流每页条数
const FOLLOW_MS = 3000;        // 「跟随最新」的轮询间隔
const PLUGIN_CACHE_MS = 3000;  // 插件列表的缓存时长，避免每次切页都重拉

// 日志类型：value 是 /api/logs 的 kinds 参数，label 与 /api/logs/counts 的键同名。
const LOG_KINDS = [
  { value: '', label: '全部', short: '全部', cls: '' },
  { value: 'receive', label: '收到', short: '收到', cls: 'k-receive' },
  { value: 'send', label: '发出', short: '发出', cls: 'k-send' },
  { value: 'preview', label: '预览', short: '预览', cls: 'k-preview' },
  { value: 'plugin', label: '插件处理', short: '处理', cls: 'k-plugin' },
  { value: 'error', label: '异常', short: '异常', cls: 'k-error' },
  { value: 'system', label: '系统', short: '系统', cls: 'k-system' },
];

const STATE_CLASS = {
  '运行中': 'st-run', '已停用': 'st-off', '未运行': 'st-idle', '已降级': 'st-bad',
  // 定时任务的状态标签：待触发是常态，不加色（用 .state 的默认样式）；未注册/未加载按停用看。
  '配置错误': 'st-bad', '未注册': 'st-off', '未加载': 'st-off',
};

const TYPE_LABEL = {
  text: '文本', password: '密码', int: '整数', float: '小数',
  bool: '开关', enum: '枚举', map: '键值表', json: '原始 JSON',
};

const state = {
  plugins: { rows: [], error: '', at: 0 },
  routeToken: 0,
  followTimer: null,
  // 管理台鉴权令牌：由启动时打印的 ?token=… 带进来（server.py 只校验 /api 路径）。
  token: new URLSearchParams(location.search).get('token') || '',
};

/* ---------- 小工具 ---------- */

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key === 'style') Object.assign(node.style, value);
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (value === false) continue;          // 布尔假属性不写进 DOM（避免 disabled="false" 反而生效）
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    }
  }
  appendAll(node, children);
  return node;
}

function appendAll(node, children) {
  if (children === null || children === undefined) return;
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/* ---------- 内联 SVG 图标（不用 emoji 当结构图标，也不引图标库） ---------- */

const SVG_NS = 'http://www.w3.org/2000/svg';

const ICON_PATHS = {
  help: 'M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17z M9.8 9.5a2.4 2.4 0 1 1 3.1 2.3c-.7.3-1 .8-1 1.4v.4 M12 16.7h.01',
  alert: 'M12 3.4 2.9 19.6h18.2L12 3.4z M12 9.7v4.1 M12 16.8h.01',
  info: 'M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17z M12 11.2v4.6 M12 8.2h.01',
  refresh: 'M20 12a8 8 0 1 1-2.6-5.9 M20.2 4.4v6.6h-6.6',
  empty: 'M3.6 13.2h4.1l1.2 2.6h6.2l1.2-2.6h4.1 M5.5 13.2 7.3 5.6h9.4l1.8 7.6v4.6a1.5 1.5 0 0 1-1.5 1.5H7a1.5 1.5 0 0 1-1.5-1.5v-4.6z',
};

function icon(name, extraClass) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.setAttribute('class', 'icon' + (extraClass ? ' ' + extraClass : ''));
  const path = document.createElementNS(SVG_NS, 'path');
  path.setAttribute('d', ICON_PATHS[name] || ICON_PATHS.info);
  svg.append(path);
  return svg;
}

// 表单控件与说明浮窗需要成对的 id，页面里递增生成即可
let uidSeq = 0;
function uid(prefix) {
  uidSeq += 1;
  return prefix + '-' + uidSeq;
}

// 显示时间统一按北京时间（框架的口径），与浏览器所在时区无关。
const BJ_FORMAT = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', hourCycle: 'h23',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

function beijingParts(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const parts = {};
  for (const part of BJ_FORMAT.formatToParts(date)) {
    if (part.type !== 'literal') parts[part.type] = part.value;
  }
  return parts;
}

function beijingToday() {
  const parts = beijingParts(new Date());
  return parts ? `${parts.year}-${parts.month}-${parts.day}` : '';
}

function fmtTime(value) {
  const parts = beijingParts(value);
  return parts ? `${parts.hour}:${parts.minute}:${parts.second}` : '—';
}

function fmtShortTime(value) {
  const parts = beijingParts(value);
  if (!parts) return '—';
  const today = beijingParts(new Date());
  if (today && parts.year === today.year && parts.month === today.month && parts.day === today.day) {
    return `${parts.hour}:${parts.minute}`;
  }
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

function num(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString('zh-CN') : '0';
}

function fmtMs(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(1)} ms` : '—';
}

/* ---------- 请求 ---------- */

async function api(path, options) {
  const init = Object.assign({}, options || {});
  init.headers = Object.assign({}, init.headers || {});
  // 鉴权：所有 /api 请求都带上令牌头（插件页面里的 fetch 用不了自定义头，走查询参数）。
  if (state.token) init.headers['X-Admin-Token'] = state.token;
  if (init.body !== undefined && typeof init.body !== 'string') {
    init.method = init.method || 'POST';
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(init.body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    return { status: 0, data: { error: String((error && error.message) || error) } };
  }
  let data = {};
  try {
    const text = await response.text();
    data = text ? JSON.parse(text) : {};
  } catch (error) {
    data = {};
  }
  return { status: response.status, data };
}

function statusHint(status) {
  if (status === 0) return '连不上管理台（服务可能已经退出）';
  if (status === 409) return '已有调试在进行，等它跑完再试';
  if (status === 503) return '统计库已关闭（管理台正在退出），暂时不可用';
  if (status === 404) return '接口不存在';
  if (status === 500) return '管理台内部错误';
  return '';
}

function backendText(result) {
  const detail = result && result.data ? result.data.error : '';
  return detail ? String(detail) : '';
}

function errorText(result, fallback) {
  const detail = backendText(result);
  const hint = statusHint(result ? result.status : 0);
  if (hint && detail) return `${hint}（后端原文：${detail}）`;
  return hint || detail || fallback || '请求失败';
}

function errorBlock(result, fallback, onRetry) {
  const detail = backendText(result);
  const hint = statusHint(result ? result.status : 0);
  // 出错一律给一个重试入口；传 false 可以显式关掉（例如就地刷新由外层自己处理）。
  const retry = onRetry === false ? null : (onRetry || route);
  if (hint && detail) return alertBox('error', 'alert', hint, '后端原文：' + detail, retry);
  return alertBox('error', 'alert', hint || detail || fallback || '请求失败', '', retry);
}

function errorBox(text, onRetry) {
  return alertBox('error', 'alert', text, '', onRetry === false ? null : (onRetry || route));
}

// 鉴权提示：没带 ?token= 打开时 /api 一律 401。重试没有意义（state.token 只在启动时读一次），
// 所以这里不给重试按钮，只把「换用带 token 的完整地址」说清楚。
function missingTokenNotice() {
  return errorBox(
    '管理台需要令牌：请用启动时打印的带 ?token=… 的完整地址打开本页，否则接口一律返回 401。',
    false,
  );
}

// 提示条统一走这里：图标 + 标题 + 可选详情 + 可选重试，语义色只由 kind 决定。
function alertBox(kind, iconName, headText, detailText, onRetry) {
  const box = el('div', { class: 'alert alert-' + kind });
  box.append(el('div', { class: 'head' }, [icon(iconName), el('span', { text: headText })]));
  if (detailText) box.append(el('div', { class: 'detail', text: detailText }));
  if (typeof onRetry === 'function') {
    const retry = el('button', { class: 'btn sm', type: 'button' }, [icon('refresh'), el('span', { text: '重试' })]);
    retry.addEventListener('click', onRetry);
    box.append(el('div', { class: 'alert-actions' }, [retry]));
  }
  return box;
}

function emptyBox(text) {
  const box = el('div', { class: 'empty' });
  box.append(icon('empty'));
  box.append(el('span', { text }));
  return box;
}

// 统一的加载态：一行状态文字 + 骨架条，读屏会念出 role=status 的内容。
function loadingBox(text, rows) {
  const box = el('div', { class: 'loading', role: 'status', 'aria-live': 'polite' });
  box.append(el('div', { class: 'loading-head' }, [
    el('span', { class: 'spinner', 'aria-hidden': 'true' }),
    el('span', { text }),
  ]));
  const skeleton = el('div', { class: 'skeleton', 'aria-hidden': 'true' });
  const count = rows || 4;
  for (let index = 0; index < count; index += 1) skeleton.append(el('div', { class: 'sk-row' }));
  box.append(skeleton);
  return box;
}

function pageTitle(title, sub) {
  const box = el('div');
  box.append(el('h1', { class: 'page-title', text: title }));
  if (sub) box.append(el('p', { class: 'page-sub', text: sub }));
  return box;
}

function tableBox(title, headers, rows, emptyText, note) {
  const box = el('section', { class: 'block' });
  box.append(el('h2', { class: 'block-title', text: title }));
  if (note) box.append(el('div', { class: 'block-note', text: note }));
  if (!rows.length) {
    box.append(emptyBox(emptyText));
    return box;
  }
  const head = el('tr', null, headers.map(header => el('th', { scope: 'col', text: header })));
  const body = el('tbody', null, rows.map(cells => el('tr', null, cells.map(cell => {
    if (cell instanceof Node) return el('td', null, cell);
    const text = cell === null || cell === undefined || cell === '' ? '—' : String(cell);
    return el('td', { text });
  }))));
  const table = el('table', { class: 'grid' });
  // 列数决定表格的最小宽度：窄屏时交给外层容器横向滚动，而不是把列挤变形。
  table.style.setProperty('--cols', headers.length);
  table.append(el('thead', null, head), body);
  box.append(el('div', { class: 'table-wrap' }, [table]));
  return box;
}

/* ---------- ❓ 浮窗 ---------- */

let popoverNode = null;
let popoverAnchor = null;

// 视口坐标定位（.popover 是 position: fixed），超出下方空间就向上翻转。
function positionPopover(panel, anchor) {
  const rect = anchor.getBoundingClientRect();
  const viewW = document.documentElement.clientWidth;
  const viewH = document.documentElement.clientHeight;
  const gap = 8;
  const margin = 8;
  const width = panel.offsetWidth;
  const height = panel.offsetHeight;

  panel.style.left = Math.max(margin, Math.min(rect.left - 40, viewW - width - margin)) + 'px';

  const roomBelow = viewH - rect.bottom - gap;
  const roomAbove = rect.top - gap;
  if (height > roomBelow && roomAbove > roomBelow) {
    panel.style.top = Math.max(margin, rect.top - height - gap) + 'px';
    panel.dataset.placement = 'top';
  } else {
    panel.style.top = Math.min(rect.bottom + gap, Math.max(margin, viewH - height - margin)) + 'px';
    panel.dataset.placement = 'bottom';
  }
}

function onPopoverViewportChange() {
  if (popoverNode && popoverAnchor && document.contains(popoverAnchor)) {
    positionPopover(popoverNode, popoverAnchor);
  }
}

function closePopover(restoreFocus) {
  if (!popoverNode) return;
  const anchor = popoverAnchor;
  popoverNode.remove();
  popoverNode = null;
  popoverAnchor = null;
  window.removeEventListener('resize', onPopoverViewportChange);
  document.removeEventListener('scroll', onPopoverViewportChange, true);
  if (anchor) {
    anchor.setAttribute('aria-expanded', 'false');
    if (restoreFocus && document.contains(anchor)) anchor.focus();
  }
}

function defaultText(field) {
  const value = field.default;
  if (value === undefined || value === null) return '（无）';
  if (value === '') return '空串（""）';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

function rangeText(field) {
  if (Array.isArray(field.range) && field.range.length === 2) return `${field.range[0]} – ${field.range[1]}`;
  if (Array.isArray(field.choices) && field.choices.length) return '可选：' + field.choices.join('、');
  switch (field.type) {
    case 'int': return '整数';
    case 'float': return '数值';
    case 'bool': return 'true / false';
    case 'map': return '键值表（键与值都是非空字符串）';
    case 'json': return '任意可 JSON 序列化的值';
    case 'password': return field.allow_empty === false ? '非空字符串' : '字符串（空串表示还没设置）';
    default: return field.allow_empty === false ? '非空字符串' : '字符串（可为空）';
  }
}

function openPopover(anchor, field) {
  closePopover(false);
  const help = field.help && String(field.help).trim() ? String(field.help) : '插件没有为这一项写说明。';
  const rows = [
    ['字段', field.key || ''],
    ['类型', TYPE_LABEL[field.type] || field.type || '未知'],
    ['默认值', defaultText(field)],
    ['取值范围', rangeText(field)],
    ['说明', help],
  ];
  const title = field.label || field.key || '字段说明';
  const panel = el('div', {
    class: 'popover',
    role: 'dialog',
    'aria-label': title + ' 的说明',
    tabindex: '-1',
  }, [
    el('div', { class: 'popover-title', text: title }),
    ...rows.map(([key, value]) => el('div', { class: 'popover-row' }, [
      el('span', { class: 'k', text: key }),
      el('span', { class: 'v', text: value }),
    ])),
  ]);
  document.body.append(panel);
  popoverNode = panel;
  popoverAnchor = anchor;
  anchor.setAttribute('aria-expanded', 'true');
  positionPopover(panel, anchor);
  window.addEventListener('resize', onPopoverViewportChange);
  document.addEventListener('scroll', onPopoverViewportChange, true);
  panel.focus();
}

document.addEventListener('click', event => {
  if (!popoverNode) return;
  if (popoverNode.contains(event.target)) return;
  if (event.target.closest && event.target.closest('.help-btn')) return;
  closePopover(false);
});

document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (popoverNode) {
    event.preventDefault();
    closePopover(true);
    return;
  }
  closeNav();
});

/* ---------- 插件列表（导航与多个页面共用） ---------- */

async function loadPlugins(force) {
  const now = Date.now();
  if (!force && state.plugins.at && now - state.plugins.at < PLUGIN_CACHE_MS) return state.plugins;
  const result = await api('/api/plugins');
  if (result.status !== 200) {
    state.plugins = { rows: [], error: errorText(result, '插件列表读取失败'), at: Date.now() };
    return state.plugins;
  }
  // 后端 /api/plugins 仍会回一个 day 字段，但页面从不渲染它，前端不再留存（死状态）。
  state.plugins = {
    rows: Array.isArray(result.data.plugins) ? result.data.plugins : [],
    error: '',
    at: Date.now(),
  };
  return state.plugins;
}

function findPlugin(name) {
  return state.plugins.rows.find(row => row.name === name) || null;
}

/* ---------- 路由与导航 ---------- */

function pluginConfigHash(name) {
  return '#/plugins/' + encodeURIComponent(name) + '/config';
}

function pluginPageHash(name, key) {
  return '#/plugins/' + encodeURIComponent(name) + '/page/' + encodeURIComponent(key);
}

function currentRoute() {
  let parts;
  try {
    parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  } catch (error) {
    // 畸形 hash（例如 %E0%A4%A）会让 decodeURIComponent 抛错，整页停在这里；回落概览。
    return { name: 'overview' };
  }
  if (parts[0] === 'plugins' && parts[1]) {
    if (parts[2] === 'page' && parts[3]) return { name: 'plugin-page', plugin: parts[1], key: parts[3] };
    return { name: 'plugin-config', plugin: parts[1] };
  }
  if (parts[0] === 'tasks') return { name: 'tasks' };
  if (parts[0] === 'debug') return { name: 'debug' };
  if (parts[0] === 'logs') return { name: 'logs' };
  return { name: 'overview' };
}

function navLink(label, href, active, extraClass) {
  const attrs = {
    class: 'nav-link' + (extraClass ? ' ' + extraClass : '') + (active ? ' active' : ''),
    href,
    text: label,
    title: label,
  };
  if (active) attrs['aria-current'] = 'page';
  return el('a', attrs);
}

function buildNav(target) {
  const nav = document.getElementById('nav-tree');
  nav.innerHTML = '';
  nav.append(navLink('概览', '#/overview', target.name === 'overview'));
  nav.append(navLink('定时任务', '#/tasks', target.name === 'tasks'));
  nav.append(el('div', { class: 'nav-group', text: '插件' }));
  if (state.plugins.error) nav.append(el('div', { class: 'nav-note', text: state.plugins.error }));
  else if (!state.plugins.rows.length) nav.append(el('div', { class: 'nav-note', text: '还没有发现插件' }));
  for (const row of state.plugins.rows) {
    const name = row.name || '';
    nav.append(navLink(name, pluginConfigHash(name), false, 'sub'));
    nav.append(navLink('配置', pluginConfigHash(name),
      target.name === 'plugin-config' && target.plugin === name, 'sub2'));
    for (const page of Array.isArray(row.pages) ? row.pages : []) {
      const key = page && page.key ? String(page.key) : '';
      if (!key) continue;
      nav.append(navLink(page.title || key, pluginPageHash(name, key),
        target.name === 'plugin-page' && target.plugin === name && target.key === key, 'sub2'));
    }
  }
  nav.append(navLink('调试', '#/debug', target.name === 'debug'));
  nav.append(navLink('日志', '#/logs', target.name === 'logs'));
}

function stopFollow() {
  if (state.followTimer) {
    clearInterval(state.followTimer);
    state.followTimer = null;
  }
}

/* ---------- 窄屏侧栏抽屉 ---------- */

const NARROW_QUERY = '(max-width: 767px)';

function isNarrow() {
  return typeof window.matchMedia === 'function' && window.matchMedia(NARROW_QUERY).matches;
}

function navParts() {
  return {
    toggle: document.getElementById('nav-toggle'),
    close: document.getElementById('nav-close'),
    scrim: document.getElementById('nav-scrim'),
  };
}

function openNav() {
  const { toggle, close, scrim } = navParts();
  document.body.classList.add('nav-open');
  if (scrim) scrim.hidden = false;
  if (toggle) {
    toggle.setAttribute('aria-expanded', 'true');
    toggle.setAttribute('aria-label', '收起导航');
  }
  // 焦点送进抽屉，键盘用户不用再从头 Tab 一遍
  if (close) close.focus();
}

function closeNav() {
  const { toggle, scrim } = navParts();
  if (!document.body.classList.contains('nav-open')) return;
  const hadFocusInside = !!document.activeElement && !!document.activeElement.closest('#sidebar');
  document.body.classList.remove('nav-open');
  if (scrim) scrim.hidden = true;
  if (toggle) {
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', '打开导航');
    // 焦点回到触发按钮，键盘用户不会掉到文档开头
    if (hadFocusInside) toggle.focus();
  }
}

function initShell() {
  const { toggle, close, scrim } = navParts();
  const skip = document.getElementById('skip-link');
  const main = document.getElementById('main');

  if (skip && main) {
    skip.addEventListener('click', () => {
      main.focus();
      main.scrollIntoView({ block: 'start' });
    });
  }
  if (toggle) {
    toggle.addEventListener('click', () => {
      if (document.body.classList.contains('nav-open')) closeNav();
      else openNav();
    });
  }
  if (close) close.addEventListener('click', () => closeNav());
  if (scrim) scrim.addEventListener('click', () => closeNav());

  // 点了导航链接就收起抽屉；点视口拉宽回桌面时也复位
  const nav = document.getElementById('nav-tree');
  if (nav) nav.addEventListener('click', event => {
    if (event.target.closest && event.target.closest('.nav-link')) closeNav();
  });
  window.addEventListener('resize', () => {
    if (!isNarrow()) {
      document.body.classList.remove('nav-open');
      const parts = navParts();
      if (parts.scrim) parts.scrim.hidden = true;
      if (parts.toggle) parts.toggle.setAttribute('aria-expanded', 'false');
    }
  });
}

/* ---------- 路由 ---------- */

function routeTitle(target) {
  if (target.name === 'plugin-config') return '插件 · ' + target.plugin;
  if (target.name === 'plugin-page') return '插件 · ' + target.plugin + ' · ' + target.key;
  if (target.name === 'tasks') return '定时任务';
  if (target.name === 'debug') return '调试';
  if (target.name === 'logs') return '日志';
  return '概览';
}

async function route() {
  const token = ++state.routeToken;
  stopFollow();
  closePopover(false);
  if (!isNarrow()) closeNav();
  const target = currentRoute();
  const title = routeTitle(target);
  document.title = title + ' · 微信机器人管理台';
  const main = document.getElementById('main');
  // 页面脚本一律 replaceChildren 自己的容器，所以提示条要放在容器「外面」，
  // 否则第一帧渲染就会把它抹掉。这里给内容加一层不带样式的 view。
  const view = el('div');
  main.replaceChildren();
  if (!state.token) main.append(missingTokenNotice());
  view.append(loadingBox('正在读取「' + title + '」…', 5));
  main.append(view);
  await loadPlugins(false);
  if (token !== state.routeToken) return;
  buildNav(target);
  if (target.name === 'plugin-config') return renderPluginConfig(view, target.plugin);
  if (target.name === 'plugin-page') return renderPluginPage(view, target.plugin, target.key);
  if (target.name === 'tasks') return renderTasks(view);
  if (target.name === 'debug') return renderDebug(view);
  if (target.name === 'logs') return renderLogs(view);
  return renderOverview(view);
}

/* ---------- 概览 ---------- */

function statCard(title, value, sub, cls) {
  return el('div', { class: 'card' + (cls ? ' ' + cls : '') }, [
    el('div', { class: 'card-title', text: title }),
    el('div', { class: 'card-value', text: value }),
    el('div', { class: 'card-sub', text: sub || '' }),
  ]);
}

function errorPluginsText() {
  if (state.plugins.error) return '';
  const rows = state.plugins.rows
    .map(row => ({ name: row.name || '', count: Number(row.today_errors) || 0 }))
    .filter(item => item.count > 0)
    .sort((left, right) => right.count - left.count);
  if (!rows.length) return '今日没有异常';
  return rows.slice(0, 3).map(item => `${item.name} ×${item.count}`).join(' · ');
}

function todayCards(data) {
  const today = (data && data.today) || {};
  const received = Number(today.received) || 0;
  const handled = Number(today.handled) || 0;
  // 「插件处理」是「消息 × 插件」的行数（每条消息分发给每个插件各记一行），
  // 拿它除以收到数得到的不是命中率，所以这里只报平均处理次数、不做百分比解读。
  const handledSub = received > 0
    ? `每条收到平均 ${(handled / received).toFixed(1)} 次插件处理`
    : '暂无收到消息，算不出平均处理次数';
  const box = el('div', { class: 'block' });
  box.append(el('div', { class: 'block-note', text: `今日（${beijingToday()}，北京时间 0 点起）` }));
  box.append(el('div', { class: 'cards' }, [
    statCard('收到', num(today.received), `群 ${num(today.received_group)} · 私聊 ${num(today.received_direct)}`, 'receive'),
    statCard('发出', num(today.sent), '', 'send'),
    statCard('插件处理', num(today.handled), handledSub, 'plugin'),
    statCard('异常', num(today.errors), errorPluginsText(), 'error'),
  ]));
  return box;
}

function totalCards(data) {
  const total = (data && data.total) || {};
  const box = el('div', { class: 'block' });
  box.append(el('div', { class: 'block-note', text: '累计（自启用管理台统计那天起算，之前的历史补不回来）' }));
  box.append(el('div', { class: 'cards' }, [
    statCard('收到', num(total.received), `群 ${num(total.received_group)} · 私聊 ${num(total.received_direct)}`, 'total'),
    statCard('发出', num(total.sent), '', 'total'),
    statCard('插件处理', num(total.handled), '按插件维度累计', 'total'),
    statCard('异常', num(total.errors), '', 'total'),
  ]));
  return box;
}

function trendLabel(day, isToday) {
  if (isToday) return '今天';
  const text = String(day || '');
  return text.length >= 10 ? text.slice(5) : text;
}

// 柱子只报告「本序列值 / 两个序列合起来的最大值」这个比例，
// 具体像素交给 CSS 的 --chart-h 换算（height: calc(--chart-h × --share)）。
function barNode(cls, value, share) {
  const bar = el('div', { class: cls });
  bar.style.setProperty('--share', share.toFixed(6));
  if (value > 0) bar.style.minHeight = '2px';   // 有值就得看得见，别被四舍五入抹掉
  return bar;
}

function trendPanel(result) {
  const panel = el('div', { class: 'panel grow' });
  panel.append(el('div', { class: 'panel-title', text: '近 7 天收到 / 发出' }));
  if (result.status !== 200) {
    panel.append(errorBlock(result, '趋势读取失败'));
    return panel;
  }
  const rows = Array.isArray(result.data) ? result.data : [];
  if (!rows.length) {
    panel.append(emptyBox('近 7 天还没有数据。'));
    return panel;
  }
  const max = rows.reduce((best, row) => Math.max(best, Number(row.received) || 0, Number(row.sent) || 0), 0);
  // 两个序列共用同一把尺子（合起来的最大值），所以收到与发出可以直接比高低。
  const shareOf = value => (max > 0 ? value / max : 0);

  const labels = rows.map((row, index) => trendLabel(row.day, index === rows.length - 1));
  const chart = el('div', { class: 'trend' });
  rows.forEach((row, index) => {
    const received = Number(row.received) || 0;
    const sent = Number(row.sent) || 0;
    chart.append(el('div', { class: 'trend-day' }, [
      el('div', { class: 'trend-track' }, [
        barNode('trend-receive', received, shareOf(received)),
        barNode('trend-send', sent, shareOf(sent)),
      ]),
      el('div', { class: 'trend-label', text: labels[index] }),
    ]));
  });
  // 图的文本替代：一句摘要给读屏，具体数字放在可展开的数据表里。
  chart.setAttribute('role', 'img');
  chart.setAttribute('aria-label', '近 7 天收到 / 发出趋势柱状图：' + rows.map((row, index) =>
    `${labels[index]} 收到 ${Number(row.received) || 0} 条、发出 ${Number(row.sent) || 0} 条`).join('；'));
  panel.append(chart);

  panel.append(el('div', { class: 'legend' }, [
    el('span', { class: 'item' }, [el('span', { class: 'swatch receive' }), el('span', { text: '收到' })]),
    el('span', { class: 'item' }, [el('span', { class: 'swatch send' }), el('span', { text: '发出' })]),
  ]));
  if (max === 0) panel.append(el('div', { class: 'block-note', text: '这 7 天还没有记录到收发消息。' }));

  const dataTable = el('table', { class: 'grid' });
  dataTable.style.setProperty('--cols', 3);
  dataTable.append(
    el('thead', null, el('tr', null, ['日期', '收到', '发出'].map(header => el('th', { scope: 'col', text: header })))),
    el('tbody', null, rows.map((row, index) => el('tr', null, [
      el('td', { text: labels[index] }),
      el('td', { text: num(row.received) }),
      el('td', { text: num(row.sent) }),
    ]))),
  );
  panel.append(el('details', { class: 'trend-data' }, [
    el('summary', { text: '看这 7 天的具体数字' }),
    el('div', { class: 'table-wrap' }, [dataTable]),
  ]));
  return panel;
}

function talkersPanel(result) {
  const panel = el('div', { class: 'panel fixed' });
  panel.append(el('div', { class: 'panel-title', text: '今天谁在聊（今日）' }));
  if (result.status !== 200) {
    panel.append(errorBlock(result, '发言人读取失败'));
    return panel;
  }
  const rows = Array.isArray(result.data) ? result.data : [];
  if (!rows.length) {
    panel.append(emptyBox('今日还没有记录到发言。'));
    return panel;
  }
  panel.append(el('div', { class: 'talkers' }, rows.map(row => el('div', { class: 'talker' }, [
    el('span', { class: 'talker-name', text: row.name || row.sender_id || '（未知）', title: row.sender_id || '' }),
    el('span', { class: 'talker-count', text: `${num(row.count)} 条` }),
  ]))));
  panel.append(el('div', { class: 'block-note', text: '按今日收到的消息数排序；只统计已启用记录的会话。' }));
  return panel;
}

function groupsTable(result) {
  if (result.status !== 200) return errorBlock(result, '各群统计读取失败');
  const rows = Array.isArray(result.data) ? result.data : [];
  const cells = rows.map(row => [
    row.chat_id || '—',
    num(row.today_received),
    num(row.today_sent),
    num(row.total_received),
    row.top_plugin || '—',
    fmtShortTime(row.last_ts),
  ]);
  return tableBox('各群', ['群', '今日收到', '今日发出', '累计收到', '最活跃插件', '最近一条'], cells,
    '今天还没有任何群的记录。',
    '「最活跃插件」按今日处理数取；「最近一条」取该群消息记录表里最新的一条。');
}

function countCell(value, today) {
  const todayCount = Number(today) || 0;
  return el('span', null, [
    el('span', { text: num(value) }),
    todayCount ? el('span', { class: 'dim', text: `（今日 ${todayCount}）` }) : null,
  ]);
}

function pluginsTable() {
  if (state.plugins.error) {
    return errorBox(state.plugins.error, () => { loadPlugins(true).then(() => route()); });
  }
  const rows = state.plugins.rows;
  const cells = rows.map(row => [
    el('span', null, [
      el('span', { text: row.name || '' }),
      row.declared ? null : el('span', { class: 'dim', text: '（未声明配置）' }),
    ]),
    el('span', { class: 'state ' + (STATE_CLASS[row.state] || ''), text: row.state || '—' }),
    countCell(row.handled, row.today_handled),
    countCell(row.errors, row.today_errors),
    num(row.dropped),
    row.error ? el('span', { class: 'dim', text: row.error }) : '—',
  ]);
  return tableBox('各插件', ['插件', '状态', '处理', '异常', '丢弃', '最近错误'], cells,
    '还没有发现插件。',
    '处理/异常/丢弃是本次启动以来的内存计数（括号里是今日数）；「最近错误」取最近一次加载或回调的错误摘要。');
}

async function renderOverview(container) {
  const token = state.routeToken;
  const [summary, groups, trend, talkers] = await Promise.all([
    api('/api/stats/summary'),
    api('/api/stats/groups?limit=50'),
    api('/api/stats/trend?days=7'),
    api('/api/stats/talkers?limit=10'),
  ]);
  if (token !== state.routeToken) return;
  const box = el('div');
  box.append(pageTitle('概览', '数字来自管理台统计库；「今日」按北京时间 0 点起算。'));
  if (summary.status !== 200) box.append(errorBlock(summary, '统计读取失败'));
  else box.append(todayCards(summary.data), totalCards(summary.data));
  box.append(el('div', { class: 'columns' }, [trendPanel(trend), talkersPanel(talkers)]));
  box.append(groupsTable(groups));
  box.append(pluginsTable());
  container.replaceChildren(box);
}

/* ---------- 插件配置 ---------- */

function jsonText(value) {
  try {
    return JSON.stringify(value === undefined ? null : value, null, 2);
  } catch (error) {
    return '';
  }
}

function buildFieldInput(field, id) {
  const type = field.type;
  if (type === 'bool') {
    const input = el('input', { type: 'checkbox', class: 'input-check', id });
    input.checked = field.value === true;
    return input;
  }
  if (type === 'enum') {
    const select = el('select', { class: 'input', id });
    const choices = Array.isArray(field.choices) ? field.choices : [];
    // 每个 option 的原始 choice 值按索引记下来：后端按严格类型比较，
    // 数字/布尔选项不能一律当字符串交回去。
    const original = [];
    const current = field.value === null || field.value === undefined ? '' : String(field.value);
    if (current === '') {
      // 当前值为空时给一个占位项，免得 select.value 落空、下拉框显示空白。
      select.append(el('option', { value: '', text: '（当前为空，未设置）' }));
      // 记住占位项的索引：它不是可选项，选中它保存等于「不动这个键」。
      select.placeholderIndex = 0;
      original.push(field.value === undefined ? null : field.value);
    }
    for (const choice of choices) {
      select.append(el('option', { value: String(choice), text: String(choice) }));
      original.push(choice);
    }
    if (current !== '' && !choices.some(choice => String(choice) === current)) {
      select.append(el('option', { value: current, text: current + '（当前值，不在声明的选项里）' }));
      original.push(field.value);
    }
    select.choiceValues = original;
    const matched = original.findIndex(value => String(value) === current);
    select.selectedIndex = matched < 0 ? 0 : matched;
    return select;
  }
  if (type === 'int' || type === 'float') {
    const input = el('input', { class: 'input', type: 'number', step: type === 'float' ? 'any' : '1', id });
    input.value = field.value === null || field.value === undefined ? '' : String(field.value);
    return input;
  }
  if (type === 'map' || type === 'json') {
    const area = el('textarea', { class: 'input input-area', rows: '4', spellcheck: 'false', id });
    area.value = jsonText(field.value);
    return area;
  }
  const input = el('input', { class: 'input', type: type === 'password' ? 'password' : 'text', id });
  input.value = field.value === null || field.value === undefined ? '' : String(field.value);
  if (type === 'password') input.placeholder = '留空表示不修改';
  return input;
}

function readFieldValue(field, input) {
  switch (field.type) {
    case 'bool':
      return input.checked;
    case 'enum': {
      // 按选中项的索引回填原始 choice 值，保留声明里的类型（数字/布尔不会被转成字符串）。
      const values = Array.isArray(input.choiceValues) ? input.choiceValues : null;
      const index = input.selectedIndex;
      // 占位项不是可选值：返回 undefined，保存时跳过这个键（「未设置」就是不写）。
      if (input.placeholderIndex === index) return undefined;
      if (values && index >= 0 && index < values.length) return values[index];
      return input.value;
    }
    case 'int':
    case 'float': {
      const raw = input.value.trim();
      // 空值交给后端报错，不在页面里静默改成 0。
      if (raw === '') return '';
      const number = Number(raw);
      return Number.isFinite(number) ? number : raw;
    }
    case 'map':
    case 'json':
      try {
        return JSON.parse(input.value);
      } catch (error) {
        // 解析不了就把原文交上去，让后端回它自己的中文错误。
        return input.value;
      }
    default:
      return input.value;
  }
}

function fieldRow(field, input) {
  const label = field.label || field.key;
  const helpBtn = el('button', {
    class: 'help-btn',
    type: 'button',
    'aria-label': '查看「' + label + '」的说明',
    'aria-haspopup': 'dialog',
    'aria-expanded': 'false',
    title: '看这一项的说明与取值范围',
  }, [icon('help')]);
  helpBtn.addEventListener('click', event => {
    event.preventDefault();
    openPopover(helpBtn, field);
  });
  const extra = field.type === 'password' ? '留空表示不修改'
    : (field.type === 'map' || field.type === 'json') ? 'JSON 格式'
      : '';
  return el('div', { class: 'field-row' }, [
    el('div', { class: 'field-label' }, [
      el('label', { class: 'field-name', for: input.id, text: label }),
      el('span', { class: 'field-key', text: field.key }),
      helpBtn,
    ]),
    el('div', { class: 'field-input' }, input),
    el('div', { class: 'field-extra', text: extra }),
  ]);
}

function degradedBanner(reason) {
  return alertBox('warn', 'alert',
    '插件已降级：它还在列表里，但加载或运行时报了错，配置可能没生效。',
    reason || '（没有给出原因）');
}

function undeclaredView(data) {
  const box = el('div', { class: 'block' });
  box.append(alertBox('info', 'info',
    '该插件没有在 plugin.json 里声明配置项，管理台不能改它。',
    '下面是 bot_config.json 里该插件的原始配置，只读；要改请直接改文件，改完用控制台的 reload 命令重载。'));
  const raw = data && data.raw && typeof data.raw === 'object' ? data.raw : {};
  box.append(el('h2', { class: 'block-title', text: '原始 JSON（只读）' }));
  box.append(el('pre', { class: 'raw-json', text: JSON.stringify(raw, null, 2) }));
  if (!Object.keys(raw).length) box.append(el('div', { class: 'block-note', text: '（该插件在 bot_config.json 里没有任何配置）' }));
  return box;
}

// 配置来源的四种情况：读取失败 / 未声明 / 来自 bot_config.json / 来自插件目录的文件。
function sourceText(result, data, declared, name) {
  if (result.status !== 200) return '配置声明读不出来：下面是后端返回的错误原文。';
  if (!declared) return '该插件没有声明配置项，只能看原始 JSON。';
  return data.source === 'bot_config'
    ? `配置来源：bot_config.json 的 plugins.${name}`
    : `配置来源：插件目录下的 ${data.file || '配置文件'}`;
}

async function renderPluginConfig(container, name) {
  const token = state.routeToken;
  const configUrl = '/api/plugins/' + encodeURIComponent(name) + '/config';
  await loadPlugins(true);
  const result = await api(configUrl);
  if (token !== state.routeToken) return;

  const row = findPlugin(name);
  const data = result.status === 200 && result.data ? result.data : {};
  const declared = data.declared === true;
  const source = sourceText(result, data, declared, name);

  const box = el('div');
  box.append(pageTitle('插件 · ' + name, source));

  const toggle = el('input', { type: 'checkbox', class: 'input-check' });
  toggle.checked = row ? row.enabled === true : false;
  toggle.disabled = !row;
  const stateHint = el('span', { class: 'hint', text: row ? `状态：${row.state || '—'}` : '插件状态读取失败，暂时不能启停' });
  const saveBtn = el('button', { class: 'btn primary', type: 'button', text: '保存并重载' });
  saveBtn.disabled = !declared;
  const msg = el('span', { class: 'inline-msg' });
  box.append(el('div', { class: 'toolbar' }, [
    el('label', { class: 'switch' }, [toggle, el('span', { text: '启用' })]),
    saveBtn,
    msg,
    stateHint,
  ]));

  const alerts = el('div');
  box.append(alerts);

  if (result.status !== 200) {
    // 配置读不出来时错误块已经带了后端原文，不再重复一条降级警告。
    box.append(errorBlock(result, '配置读取失败'));
    container.replaceChildren(box);
    return;
  }
  // 只有真降级（加载失败/缺能力/声明坏）才提示；回调偶发的「最近错误」不再弹黄条。
  if (row && row.degraded) alerts.append(degradedBanner(row.error));

  toggle.addEventListener('change', async () => {
    const want = toggle.checked;
    toggle.disabled = true;
    const response = await api('/api/plugins/' + encodeURIComponent(name) + '/toggle', { body: { enabled: want } });
    toggle.disabled = false;
    if (response.status !== 200) {
      toggle.checked = !want;
      msg.className = 'inline-msg bad';
      msg.textContent = errorText(response, '启停失败');
      return;
    }
    msg.className = 'inline-msg ok';
    msg.textContent = want ? '已启用并加载' : '已停用';
    await loadPlugins(true);
    buildNav(currentRoute());
    const fresh = findPlugin(name);
    stateHint.textContent = fresh ? `状态：${fresh.state || '—'}` : '状态：—';
    alerts.innerHTML = '';
    if (response.data && response.data.degraded) alerts.append(degradedBanner(response.data.reason));
  });

  if (!declared) {
    box.append(undeclaredView(data));
    container.replaceChildren(box);
    return;
  }

  const entries = [];
  const sections = Array.isArray(data.sections) ? data.sections : [];
  let maskedCount = 0;
  for (const section of sections) {
    box.append(el('h2', { class: 'section-title', text: section.title || '（未命名分区）' }));
    for (const field of Array.isArray(section.fields) ? section.fields : []) {
      if (field.masked) maskedCount += 1;
      const input = buildFieldInput(field, uid('field'));
      entries.push({ field, input });
      box.append(fieldRow(field, input));
    }
  }
  if (!entries.length) box.append(emptyBox('该插件声明了配置，但没有声明任何字段。'));
  if (maskedCount) box.append(el('div', { class: 'block-note', text: '密码类字段不回显明文，留空表示保持原值不变。' }));

  saveBtn.addEventListener('click', async () => {
    const values = {};
    for (const entry of entries) {
      const value = readFieldValue(entry.field, entry.input);
      // 未设置的项直接不写：undefined 表示「不动这个键」，而不是送一个空值过去。
      if (value === undefined) continue;
      values[entry.field.key] = value;
    }
    saveBtn.disabled = true;
    msg.className = 'inline-msg';
    msg.textContent = '保存中…';
    const response = await api(configUrl, { body: { values, reload: true } });
    saveBtn.disabled = false;
    if (response.status !== 200) {
      msg.className = 'inline-msg bad';
      msg.textContent = errorText(response, '保存失败');
      return;
    }
    msg.className = 'inline-msg ok';
    msg.textContent = '已保存并重载';
    // 密码字段不回显：存完就清空，回到「留空表示不修改」的状态。
    for (const entry of entries) if (entry.field.masked) entry.input.value = '';
    alerts.innerHTML = '';
    if (response.data && response.data.degraded) alerts.append(degradedBanner(response.data.reason));
    await loadPlugins(true);
    buildNav(currentRoute());
    const fresh = findPlugin(name);
    stateHint.textContent = fresh ? `状态：${fresh.state || '—'}` : '状态：—';
  });

  container.replaceChildren(box);
}

/* ---------- 插件自带页面 ---------- */

function pluginPageTitle(name, key) {
  const row = findPlugin(name);
  if (row) {
    for (const page of Array.isArray(row.pages) ? row.pages : []) {
      if (page && String(page.key) === key) return page.title || key;
    }
  }
  return key;
}

async function renderPluginPage(container, name, key) {
  const token = state.routeToken;
  const url = '/plugin/' + encodeURIComponent(name) + '/' + String(key).split('/').map(encodeURIComponent).join('/');
  let probe = null;
  try {
    probe = await fetch(url, { method: 'HEAD' });
  } catch (error) {
    probe = null;
  }
  if (token !== state.routeToken) return;

  const box = el('div');
  box.append(pageTitle('插件 · ' + name + ' · ' + pluginPageTitle(name, key),
    '这一页由插件自己提供；框架只负责挂载它，并把 /api/plugin/<插件名>/<动作> 转给插件。'));
  if (!probe) {
    box.append(errorBox('取不到插件页面（连不上管理台）：' + url, () => renderPluginPage(container, name, key)));
  } else if (!probe.ok) {
    box.append(errorBox(`插件页面不存在（HTTP ${probe.status}）：${url}；请确认插件目录下 web/ 里有这个文件。`, false));
  } else {
    // 插件页面里的 fetch 也要带令牌，否则它的 /api 调用会被 401。
    // 「在新标签页打开」走的是同一个地址，同样要带，否则新标签页是坏的。
    const frameSrc = state.token
      ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(state.token)
      : url;
    box.append(el('div', { class: 'block-note' }, [
      el('span', { text: '页面地址：' + url + '　' }),
      el('a', { href: frameSrc, target: '_blank', text: '在新标签页打开' }),
    ]));
    box.append(el('iframe', { class: 'plugin-frame', src: frameSrc, title: name + ' · ' + key }));
  }
  container.replaceChildren(box);
}

/* ---------- 调试 ---------- */

function renderDebugResult(box, data, note) {
  box.innerHTML = '';
  const entries = Array.isArray(data.plugins) ? data.plugins : [];
  const real = data.sent_for_real === true;
  box.append(el('div', { class: 'result-summary', text: `分发给 ${num(data.dispatched)} 个插件，其中 ${num(data.replied)} 个命中；用时 ${fmtMs(data.ms)}。${real ? '回复已真实发出。' : '本次只预览，回复没有发出去。'}` }));

  const hits = entries.filter(entry => entry.state === 'replied');
  for (const entry of hits) {
    box.append(el('div', { class: 'result-hit' }, [
      el('div', { class: 'hit-name', text: `${entry.name}　命中（${fmtMs(entry.ms)}）` }),
      el('div', { text: real ? '已真实发出：' : '会回复（本次只预览，未发出）：' }),
      el('div', { class: 'reply-box', text: entry.reply || '（回复内容为空）' }),
    ]));
  }
  if (!hits.length) box.append(emptyBox(real ? '没有插件回复。' : '没有插件会回复。'));

  const silent = entries.filter(entry => entry.state === 'silent').map(entry => entry.name);
  if (silent.length) box.append(el('div', { class: 'result-silent', text: '看过这条消息，但没有回复：' + silent.join(' · ') }));
  const skipped = entries.filter(entry => entry.state === 'skipped').map(entry => entry.name);
  if (skipped.length) box.append(el('div', { class: 'result-silent', text: '没有参与处理（未在运行或队列已满被丢弃）：' + skipped.join(' · ') }));

  for (const entry of entries.filter(item => item.state === 'error')) {
    const card = el('div', { class: 'result-error' }, [
      el('div', { class: 'err-name', text: `${entry.name}　异常` }),
      el('div', { text: entry.error || '（没有错误摘要）' }),
    ]);
    card.append(stackBlock(entry));
    box.append(card);
  }
  if (note) box.append(el('div', { class: 'block-note', text: note }));
}

// 堆栈默认折叠，点一下展开；文本用 textContent 渲染，不拼 HTML。
function stackBlock(entry) {
  const stack = typeof entry.traceback === 'string' ? entry.traceback.trim() : '';
  if (!stack) {
    return el('div', { class: 'detail', text: '这一条没有堆栈（例如插件超时）；完整堆栈见 logs/bot.log。' });
  }
  const pre = el('pre', { class: 'stack-box', text: stack, hidden: true });
  const toggle = el('button', { class: 'btn stack-toggle', type: 'button', text: '展开堆栈' });
  toggle.addEventListener('click', () => {
    const wasHidden = pre.hidden;
    pre.hidden = !wasHidden;
    toggle.textContent = wasHidden ? '收起堆栈' : '展开堆栈';
  });
  return el('div', { class: 'stack-wrap' }, [
    el('div', { class: 'stack-head' }, [
      toggle,
      el('span', { class: 'detail', text: '同一份堆栈也写在 logs/bot.log 里。' }),
    ]),
    pre,
  ]);
}

async function renderDebug(container) {
  const token = state.routeToken;
  await loadPlugins(false);
  if (token !== state.routeToken) return;

  const box = el('div');
  box.append(pageTitle('调试', '喂一条假消息走一遍插件分发，看它会做什么。'));
  box.append(alertBox('warn', 'alert',
    '调试会真实调用外部服务（可能产生费用或触发限流），也会改动插件的内部状态。',
    '调试只走插件分发，不经过白名单、管理员拦截与人格切换，命中情况可能比真实运行时乐观；「真的发到微信」默认关闭，刷新页面后复位。'));

  const pluginSelect = el('select', { class: 'input', id: 'dbg-plugin' });
  pluginSelect.append(el('option', { value: '', text: '全部插件' }));
  for (const row of state.plugins.rows) pluginSelect.append(el('option', { value: row.name, text: row.name }));

  const chatInput = el('input', { class: 'input', type: 'text', id: 'dbg-chat', placeholder: '例如 12345678@chatroom；私聊填 wxid_xxx' });
  const senderInput = el('input', { class: 'input', type: 'text', id: 'dbg-sender', placeholder: '例如 wxid_xxx（可留空）' });
  const textInput = el('textarea', { class: 'input input-area', rows: '4', id: 'dbg-text', placeholder: '例如 /bga排行榜' });
  const realCheck = el('input', { type: 'checkbox', class: 'input-check' });
  const reloadCheck = el('input', { type: 'checkbox', class: 'input-check' });
  const runBtn = el('button', { class: 'btn primary', type: 'button', text: '跑一遍' });
  const formMsg = el('span', { class: 'inline-msg', role: 'status' });
  const resultBox = el('div', { class: 'debug-result' });

  const form = el('div', { class: 'debug-form' }, [
    el('div', { class: 'form-row' }, [el('label', { for: 'dbg-plugin', text: '插件' }), pluginSelect]),
    el('div', { class: 'form-row' }, [el('label', { for: 'dbg-chat', text: '会话 ID' }), chatInput]),
    el('div', { class: 'form-row' }, [el('label', { for: 'dbg-sender', text: '发送者' }), senderInput]),
    el('div', { class: 'form-row' }, [el('label', { for: 'dbg-text', text: '内容' }), textInput]),
    el('div', { class: 'debug-actions' }, [runBtn, formMsg]),
    el('div', { class: 'debug-switches' }, [
      el('label', { class: 'switch' }, [realCheck, el('span', { text: '真的发到微信' })]),
      el('label', { class: 'switch' }, [reloadCheck, el('span', { text: '跑完自动重载插件' })]),
    ]),
  ]);
  resultBox.append(emptyBox('填好左边，点「跑一遍」看结果。'));
  box.append(el('div', { class: 'debug-layout' }, [form, resultBox]));
  container.replaceChildren(box);

  runBtn.addEventListener('click', async () => {
    const chatId = chatInput.value.trim();
    const text = textInput.value;
    if (!chatId) {
      formMsg.className = 'inline-msg bad';
      formMsg.textContent = '请先填会话 ID';
      return;
    }
    if (!text.trim()) {
      formMsg.className = 'inline-msg bad';
      formMsg.textContent = '请先填内容';
      return;
    }
    formMsg.className = 'inline-msg';
    formMsg.textContent = '跑着呢…';
    runBtn.disabled = true;
    resultBox.innerHTML = '';
    resultBox.append(loadingBox('正在跑一遍，插件可能真的访问外部服务，最长等 30 秒…', 3));
    const response = await api('/api/debug', {
      body: {
        plugin: pluginSelect.value,
        chat_id: chatId,
        sender_id: senderInput.value.trim(),
        text,
        send_for_real: realCheck.checked,
      },
    });
    runBtn.disabled = false;
    if (token !== state.routeToken) return;
    if (response.status !== 200) {
      formMsg.className = 'inline-msg bad';
      formMsg.textContent = '跑失败';
      resultBox.innerHTML = '';
      resultBox.append(errorBlock(response, '调试失败'));
      return;
    }
    formMsg.className = 'inline-msg ok';
    formMsg.textContent = '跑完了';
    let note = '';
    if (reloadCheck.checked) {
      const target = pluginSelect.value;
      const reloadUrl = target
        ? '/api/plugins/' + encodeURIComponent(target) + '/reload'
        : '/api/plugins/reload';
      const reload = await api(reloadUrl, { body: {} });
      if (token !== state.routeToken) return;
      note = reload.status === 200
        ? `已自动重载：${target || '全部插件'}`
        : `自动重载失败：${errorText(reload, '重载失败')}`;
      await loadPlugins(true);
      if (token !== state.routeToken) return;
      buildNav(currentRoute());
    }
    renderDebugResult(resultBox, response.data || {}, note);
  });
}

/* ---------- 定时任务 ---------- */

const TASK_SOURCE_LABEL = { code: '代码默认', config: '配置覆盖' };

function taskSourceCell(row) {
  // 生效时间的来源：插件代码里的默认值，还是 bot_config.json 里改过的覆盖值。
  return el('span', { class: 'dim', text: `（${TASK_SOURCE_LABEL[row.source] || '未知来源'}）` });
}

function taskStateCell(row) {
  const box = el('div');
  box.append(el('span', { class: 'state ' + (STATE_CLASS[row.state] || ''), text: row.state || '—' }));
  // 「配置错误」时把后端给的原文摆出来（例如「每天 25:00」），否则用户只看到状态不知道错在哪。
  if (row.config_error) box.append(el('div', { class: 'task-error', text: row.config_error }));
  return box;
}

function taskLastRunCell(row) {
  const box = el('div');
  box.append(el('span', { text: row.last_run_at ? fmtShortTime(row.last_run_at) : '—' }));
  // last_error 只在最近一次运行失败时非空（下一次运行成功会被清掉），照原文显示。
  if (row.last_error) box.append(el('div', { class: 'task-error', text: row.last_error }));
  return box;
}

function taskActionsCell(row, onToggle, onSchedule, onRun) {
  const toggle = el('input', { type: 'checkbox', class: 'input-check', title: '启用 / 停用这个任务' });
  toggle.checked = row.enabled === true;
  toggle.addEventListener('change', () => onToggle(row, toggle));

  const scheduleInput = el('input', {
    class: 'input', type: 'text', placeholder: '每天 09:00',
    title: '语义化写法（每天 09:00 / 工作日 09:00 / 每 30 分钟）或 5 字段 cron',
    'aria-label': '生效时间',
  });
  scheduleInput.value = row.schedule || '';
  const saveBtn = el('button', { class: 'btn', type: 'button', text: '保存时间' });
  saveBtn.addEventListener('click', () => onSchedule(row, scheduleInput, saveBtn));

  const runBtn = el('button', { class: 'btn', type: 'button', text: '跑一次' });
  runBtn.addEventListener('click', () => onRun(row, runBtn));

  return el('div', null, [
    el('div', { class: 'task-row' }, [
      el('label', { class: 'switch' }, [toggle, el('span', { text: '启用' })]),
      runBtn,
    ]),
    el('div', { class: 'task-row' }, [scheduleInput, saveBtn]),
  ]);
}

async function renderTasks(container) {
  const token = state.routeToken;
  const alerts = el('div');
  const slot = el('div');
  const box = el('div');
  box.append(pageTitle('定时任务', '到点由框架投递到所属插件自己的线程，和它的消息回调共用同一把锁。'));
  box.append(alerts, slot);
  slot.append(loadingBox('正在读取定时任务…', 4));
  container.replaceChildren(box);

  function actionUrl(row, action) {
    return '/api/tasks/' + encodeURIComponent(row.plugin) + '/' + encodeURIComponent(row.name) + '/' + action;
  }

  function taskTable(rows) {
    const cells = rows.map(row => [
      row.plugin || '',
      row.name || '',
      row.schedule || '',
      taskSourceCell(row),
      taskStateCell(row),
      taskLastRunCell(row),
      num(row.skipped),
      fmtShortTime(row.next_due),
      taskActionsCell(row, doToggle, doSchedule, doRun),
    ]);
    return tableBox('全部任务',
      ['插件', '任务', '生效时间', '来源', '状态', '上次运行', '跳过', '下次触发', '操作'], cells,
      '还没有定时任务：插件还没有注册任何任务，或插件未加载。',
      '「来源」标注生效时间从哪来；「下次触发」为 — 表示这个表达式永远不会匹配（例如 0 0 30 2 *）。');
  }

  // 成功后就地重拉 /api/tasks 刷新整张表；失败只把后端原文摆出来，不动表。
  async function rebuild() {
    const result = await api('/api/tasks');
    if (token !== state.routeToken) return;
    if (result.status !== 200) {
      alerts.innerHTML = '';
      alerts.append(errorBlock(result, '定时任务读取失败'));
      // 旧表里的按钮对应的是过期状态，读取失败后不能再点。
      slot.innerHTML = '';
      return;
    }
    const rows = Array.isArray(result.data.tasks) ? result.data.tasks : [];
    slot.innerHTML = '';
    slot.append(taskTable(rows));
  }

  async function doToggle(row, input) {
    const want = input.checked;
    input.disabled = true;
    alerts.innerHTML = '';
    const result = await api(actionUrl(row, 'toggle'), { body: { enabled: want } });
    input.disabled = false;
    if (token !== state.routeToken) return;
    if (result.status !== 200) {
      input.checked = !want;
      alerts.append(errorBlock(result, '启停失败'));
      return;
    }
    alerts.append(alertBox('info', 'info', `${want ? '已启用' : '已停用'}：${row.plugin}/${row.name}`));
    await rebuild();
  }

  async function doSchedule(row, input, button) {
    const text = input.value.trim();
    button.disabled = true;
    alerts.innerHTML = '';
    const result = await api(actionUrl(row, 'schedule'), { body: { schedule: text } });
    button.disabled = false;
    if (token !== state.routeToken) return;
    if (result.status !== 200) {
      alerts.append(errorBlock(result, '保存时间失败'));
      return;
    }
    alerts.append(alertBox('info', 'info', `已保存生效时间：${row.plugin}/${row.name} → ${text}`));
    await rebuild();
  }

  async function doRun(row, button) {
    button.disabled = true;
    alerts.innerHTML = '';
    // 必须带 body：这是 api() 判定「发 POST」的唯一开关（reload 等动作同样用 { body: {} }）。
    const result = await api(actionUrl(row, 'run'), { body: {} });
    button.disabled = false;
    if (token !== state.routeToken) return;
    if (result.status !== 200) {
      alerts.append(errorBlock(result, '跑一次失败'));
      return;
    }
    // 200 只说明请求收下了：dispatched 为假表示投递被拒（上一轮还在跑、队列拒绝），这次没排上。
    if (result.data.dispatched !== true) {
      alerts.append(alertBox('warn', 'alert',
        `任务未能投递：${row.plugin}/${row.name} 这一次没有被排上（上一轮还在跑，或队列拒绝了它）。`,
        '它现在不会执行；等这一轮跑完，再点一次「跑一次」。'));
      return;
    }
    alerts.append(alertBox('info', 'info', `已投递：${row.plugin}/${row.name} 正在插件自己的线程上跑。`));
    await rebuild();
  }

  await rebuild();
}

/* ---------- 日志 ---------- */

function kindOf(value) {
  return LOG_KINDS.find(kind => kind.value === value)
    || { value: value || '', label: value || '未知', short: value || '未知', cls: '' };
}

function logRow(row) {
  const kind = kindOf(row.kind);
  const where = row.plugin ? row.plugin : (row.chat_id || (row.kind === 'system' ? '框架' : '—'));
  return el('div', { class: 'log-row ' + kind.cls }, [
    el('span', { class: 'log-time', text: fmtTime(row.ts_utc) }),
    el('span', { class: 'log-kind ' + kind.cls, text: kind.short }),
    el('span', { class: 'log-where', text: where, title: row.chat_id || '' }),
    el('span', { class: 'log-text', text: row.summary || '' }),
  ]);
}

function toUtcIso(localValue) {
  if (!localValue) return '';
  // datetime-local 的值按北京时间理解，转成后端比较用的 UTC ISO 字符串。
  const text = localValue.length === 16 ? localValue + ':00' : localValue;
  const date = new Date(text + '+08:00');
  return Number.isNaN(date.getTime()) ? '' : date.toISOString();
}

function logsUrl(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === '' || value === null || value === undefined) continue;
    search.set(key, String(value));
  }
  return '/api/logs?' + search.toString();
}

async function renderLogs(container) {
  const token = state.routeToken;
  // 只有类型筛选需要留在状态里；其余筛选值直接从各自的输入控件读。
  const filters = { kind: '' };
  const view = { offset: 0, total: 0, following: true, limit: LOG_PAGE_SIZE };

  const kindsPanel = el('div', { class: 'logs-kinds', role: 'group', 'aria-label': '日志类型' });
  const stream = el('div', { class: 'log-stream' });
  const pageInfo = el('span');
  const prevBtn = el('button', { class: 'btn', type: 'button', text: '上一页' });
  const nextBtn = el('button', { class: 'btn', type: 'button', text: '下一页' });

  const searchInput = el('input', { class: 'input', type: 'text', id: 'log-keyword', placeholder: '搜内容 / 群 ID' });
  const chatInput = el('input', { class: 'input', type: 'text', id: 'log-chat', placeholder: '可留空' });
  const pluginSelect = el('select', { class: 'input', id: 'log-plugin' });
  pluginSelect.append(el('option', { value: '', text: '全部插件' }));
  for (const row of state.plugins.rows) pluginSelect.append(el('option', { value: row.name, text: row.name }));
  const sinceInput = el('input', { class: 'input', type: 'datetime-local', id: 'log-since', title: '起始时间（北京时间）' });
  const untilInput = el('input', { class: 'input', type: 'datetime-local', id: 'log-until', title: '结束时间（北京时间）' });
  const followCheck = el('input', { type: 'checkbox', class: 'input-check' });
  followCheck.checked = true;

  // 筛选条：每个控件都有可见标签，标签与控件用 for/id 关联
  function filterField(labelText, control) {
    return el('div', { class: 'filter-field' }, [
      el('label', { for: control.id, text: labelText }),
      control,
    ]);
  }

  const filtersBar = el('div', { class: 'filters' }, [
    filterField('搜索内容 / 群 ID', searchInput),
    filterField('群 ID', chatInput),
    filterField('插件', pluginSelect),
    filterField('起始时间（北京时间）', sinceInput),
    filterField('结束时间（北京时间）', untilInput),
    el('div', { class: 'filter-field is-switch' }, [
      el('label', { class: 'switch' }, [followCheck, el('span', { text: '跟随最新' })]),
    ]),
  ]);

  const layout = el('div', { class: 'logs-layout' }, [
    kindsPanel,
    el('div', { class: 'logs-main' }, [
      filtersBar,
      stream,
      el('div', { class: 'pager' }, [prevBtn, nextBtn, pageInfo]),
    ]),
  ]);
  stream.append(loadingBox('正在读取日志…', 6));
  container.replaceChildren(pageTitle('日志', '一条消息在框架里走过的每一步；类型底色与左侧条数对应。'));
  container.append(layout);

  const kindsNote = el('div', { class: 'nav-note', hidden: true });
  const kindButtons = [];

  // 类型筛选做成真正的按钮：可 Tab 到、可 Enter/Space 触发，选中态用 aria-pressed 表达。
  function buildKinds() {
    kindsPanel.replaceChildren();
    for (const kind of LOG_KINDS) {
      const count = el('span', { class: 'count', text: '—' });
      const item = el('button', {
        class: 'log-kind-item' + (kind.cls ? ' ' + kind.cls : ''),
        type: 'button',
        'aria-pressed': kind.value === filters.kind ? 'true' : 'false',
      }, [el('span', { text: kind.label }), count]);
      item.addEventListener('click', () => {
        filters.kind = kind.value;
        view.offset = 0;
        syncKinds(null, null);
        refresh();
      });
      kindButtons.push({ kind, item, count });
      kindsPanel.append(item);
    }
    kindsPanel.append(kindsNote);
  }

  // 就地更新条数与选中态：不重建 DOM，键盘焦点不会被「跟随最新」的定时刷新吃掉。
  function syncKinds(counts, errorResult) {
    for (const entry of kindButtons) {
      entry.item.setAttribute('aria-pressed', entry.kind.value === filters.kind ? 'true' : 'false');
      if (counts) entry.count.textContent = num(counts[entry.kind.label] || 0);
      else if (errorResult) entry.count.textContent = '—';
    }
    if (errorResult) {
      kindsNote.hidden = !!counts;
      if (!counts) kindsNote.textContent = '条数读取失败：' + errorText(errorResult, '');
    }
  }

  function updatePager() {
    const pages = Math.max(1, Math.ceil(view.total / view.limit));
    const current = Math.floor(view.offset / view.limit) + 1;
    pageInfo.textContent = `共 ${num(view.total)} 条，第 ${current} / ${pages} 页` + (view.following ? '（跟随最新中，翻页已暂停）' : '');
    prevBtn.disabled = view.following || view.offset <= 0;
    nextBtn.disabled = view.following || view.offset + view.limit >= view.total;
  }

  let refreshing = false;
  async function refresh() {
    // 跟随模式每 3 秒一轮：上一轮还没回来就跳过这一轮，免得旧响应盖掉新渲染。
    if (refreshing) return;
    refreshing = true;
    let page;
    let counts;
    try {
      [page, counts] = await Promise.all([
        api(logsUrl({
          kinds: filters.kind,
          keyword: searchInput.value.trim(),
          chat_id: chatInput.value.trim(),
          plugin: pluginSelect.value,
          since: toUtcIso(sinceInput.value),
          until: toUtcIso(untilInput.value),
          limit: view.limit,
          offset: view.offset,
        })),
        api('/api/logs/counts'),
      ]);
    } finally {
      refreshing = false;
    }
    if (token !== state.routeToken) return;
    syncKinds(counts.status === 200 && counts.data && counts.data.counts ? counts.data.counts : null, counts);
    if (page.status !== 200) {
      stream.innerHTML = '';
      stream.append(errorBlock(page, '日志读取失败', () => refresh()));
      pageInfo.textContent = '';
      prevBtn.disabled = true;
      nextBtn.disabled = true;
      return;
    }
    view.total = Number(page.data.total) || 0;
    const rows = Array.isArray(page.data.rows) ? page.data.rows : [];
    stream.innerHTML = '';
    if (!rows.length) stream.append(emptyBox('没有符合条件的记录。'));
    else for (const row of rows) stream.append(logRow(row));
    updatePager();
  }

  function applyFollowing() {
    stopFollow();
    if (view.following) {
      view.offset = 0;
      state.followTimer = setInterval(() => {
        if (token !== state.routeToken) {
          stopFollow();
          return;
        }
        refresh();
      }, FOLLOW_MS);
    }
    updatePager();
  }

  let searchTimer = null;
  searchInput.addEventListener('input', () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      view.offset = 0;
      refresh();
    }, 300);
  });
  chatInput.addEventListener('change', () => { view.offset = 0; refresh(); });
  pluginSelect.addEventListener('change', () => { view.offset = 0; refresh(); });
  sinceInput.addEventListener('change', () => { view.offset = 0; refresh(); });
  untilInput.addEventListener('change', () => { view.offset = 0; refresh(); });
  followCheck.addEventListener('change', () => {
    view.following = followCheck.checked;
    applyFollowing();
    refresh();
  });
  prevBtn.addEventListener('click', () => {
    view.offset = Math.max(0, view.offset - view.limit);
    refresh();
  });
  nextBtn.addEventListener('click', () => {
    view.offset = view.offset + view.limit;
    refresh();
  });

  buildKinds();
  await refresh();
  if (token !== state.routeToken) return;
  applyFollowing();
}

/* ---------- 启动 ---------- */

function showJsError(text) {
  const node = document.getElementById('js-error');
  node.hidden = false;
  node.textContent = '页面脚本报错：' + text;
}

window.addEventListener('error', event => showJsError(event.message || String(event.error || '未知错误')));
window.addEventListener('unhandledrejection', event => {
  showJsError(String((event.reason && event.reason.message) || event.reason || '未知错误'));
});
window.addEventListener('hashchange', () => { route(); });
initShell();
route();
