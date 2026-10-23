'use strict';

/* 管理台前端：哈希路由 + 原生 fetch，无框架、无构建、无第三方依赖。
   接口契约来自 wxbot_framework/admin/api.py，静态资源规则来自 server.py：
   - 管理台自己的页面挂在 /（就是本目录）；
   - 插件自带的页面挂在 /plugin/<插件名>/<插件内路径>，数据在 /api/plugin/<插件名>/<动作>。 */

const LOG_PAGE_SIZE = 50;      // 日志流每页条数
const REPLY_PEEK_CHARS = 40;   // 折叠行里回复预览的字数上限
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

// 日志页的两个视图：默认「按消息」（一条群消息一行），「按记录」是原来的扁平行流。
const LOG_VIEWS = [
  {
    value: 'messages',
    label: '按消息',
    note: '一条群消息一行，点开看这条消息触发的各个插件；默认只看群消息，「含私聊」带上私聊，「含系统记录」带上框架自己的记录。',
  },
  {
    value: 'records',
    label: '按记录',
    note: '原来的扁平行流：每条日志一行，左侧按类型筛选并统计条数。',
  },
];

// 按消息视图的快捷筛选：值直接发给 /api/logs/messages 的 kinds 参数
// （口径是「组内存在这类记录」，所以「有回复」就是组内有 send/preview）。
const MSG_FACETS = [
  { value: '', label: '全部' },
  { value: 'plugin', label: '有插件处理' },
  { value: 'send,preview', label: '有回复' },
  { value: 'error', label: '有异常' },
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
  caret: 'M9.5 6.5 15 12l-5.5 5.5',
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
  const box = el('div', { class: 'page-head' });
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
  if (parts[0] === 'lurkers') return { name: 'lurkers' };
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
  nav.append(navLink('潜水名单', '#/lurkers', target.name === 'lurkers'));
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

  const themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    const syncTheme = () => {
      const dark = document.documentElement.dataset.theme === 'dark';
      themeToggle.setAttribute('aria-pressed', String(dark));
      themeToggle.setAttribute('aria-label', dark ? '切换到浅色主题' : '切换到深色主题');
      themeToggle.title = dark ? '切换到浅色主题' : '切换到深色主题';
    };
    syncTheme();
    themeToggle.addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      try {
        localStorage.setItem('admin-theme', next);
      } catch (_) {
        // 隐私模式禁用 localStorage 时仍允许本次切换。
      }
      syncTheme();
    });
  }

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
  if (target.name === 'lurkers') return '潜水名单';
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
  if (target.name === 'lurkers') return renderLurkers(view);
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
  // 只统计真正发出回复的插件回调；未回复的只写日志，不计入“插件回复”。
  const handledSub = received > 0
    ? `每条收到平均 ${(handled / received).toFixed(1)} 次插件回复`
    : '暂无收到消息，算不出平均回复次数';
  const box = el('div', { class: 'block' });
  box.append(el('div', { class: 'block-note', text: `今日（${beijingToday()}，北京时间 0 点起）` }));
  box.append(el('div', { class: 'cards' }, [
    statCard('收到', num(today.received), `群 ${num(today.received_group)} · 私聊 ${num(today.received_direct)}`, 'receive'),
    statCard('发出', num(today.sent), '', 'send'),
    statCard('插件回复', num(today.handled), handledSub, 'plugin'),
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
    statCard('插件回复', num(total.handled), '按插件维度累计（仅回复）', 'total'),
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

// 今天谁在聊：一行一个人。name 是后端在该成员「代表群」里解析好的显示名（群昵称优先），
// chat_name 是那个群的群名——有群名才在名字后面挂一颗弱化的小标签，让人看得出这个名字属于哪个群；
// 群名拿不到就不挂标签（不留空位、不拼假群名），跟以前一样只显示名字。
// 悬浮提示里始终留着 wxid（有群的再带上群 id），方便对回原生 id。
function talkerRow(row) {
  const senderId = String(row.sender_id || '');
  const chatId = String(row.chat_id || '');
  const chatName = String(row.chat_name || '').trim();

  const tips = [];
  if (senderId) tips.push(senderId);
  if (chatName) tips.push(`发言最多的群：${chatName}${chatId ? `（${chatId}）` : ''}`);
  else if (chatId) tips.push(chatId);

  return el('div', { class: 'talker' }, [
    el('span', { class: 'talker-main' }, [
      el('span', { class: 'talker-name', text: row.name || senderId || '（未知）', title: tips.join('\n') }),
      chatName ? el('span', {
        class: 'talker-chat',
        text: chatName,
        title: [chatName, chatId].filter(Boolean).join(' · '),
      }) : null,
    ]),
    el('span', { class: 'talker-count', text: `${num(row.count)} 条` }),
  ]);
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
  panel.append(el('div', { class: 'talkers' }, rows.map(talkerRow)));
  panel.append(el('div', { class: 'block-note', text: '按今日收到的消息数排序；同一个人跨群合并，小标签是他今天发言最多的群。只统计已启用记录的会话。' }));
  return panel;
}

// 各群列表的「群」列：有群名就显示群名，悬浮提示里补上 chat_id 便于对回原生 id；
// 没拿到群名时维持原样直接显示 chat_id，不自己拼假名字。
function groupNameCell(row) {
  const name = String(row.chat_name || '').trim();
  if (name) return el('span', { text: name, title: row.chat_id || '' });
  return row.chat_id || '—';
}

function groupsTable(result) {
  if (result.status !== 200) return errorBlock(result, '各群统计读取失败');
  const rows = Array.isArray(result.data) ? result.data : [];
  const cells = rows.map(row => [
    groupNameCell(row),
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
  return tableBox('各插件', ['插件', '状态', '回复', '异常', '丢弃', '最近错误'], cells,
    '还没有发现插件。',
    '回复/异常/丢弃是本次启动以来的内存计数（括号里是今日数）；未回复的插件只写日志，不计入回复数；「最近错误」取最近一次加载或回调的错误摘要。');
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

/* ---------- 潜水名单 ---------- */

// 昵称为空串的成员仍算在名单里：昵称格标「（未知）」，再补一行成员 ID 尾号，
// 方便在没有名字时也能对回具体的人。
function lurkerNameCell(row) {
  const name = String(row.name || '').trim();
  const wxid = String(row.wxid || '');
  if (name) return el('span', { text: name, title: wxid || '' });
  const cell = el('div');
  cell.append(el('span', { text: '（未知）' }));
  if (wxid) {
    const tail = wxid.length > 6 ? wxid.slice(-6) : wxid;
    cell.append(el('span', { class: 'lurker-alias', text: 'ID 尾号 ' + tail, title: wxid }));
  }
  return cell;
}

function lurkerTime(value) {
  // 旧库的 first_seen 可能是 null；直接交给 fmtShortTime 会把它当成 1970 年。
  return value ? fmtShortTime(value) : '—';
}

// 名单没刷新过（roster_total 为 0 且没有任何刷新时间）≠ 群里没人；两条判定共用这一处。
function lurkerNotReady(data) {
  return (Number(data.roster_total) || 0) === 0 && !data.as_of;
}

function lurkerSummary(data) {
  const total = Number(data.roster_total) || 0;
  const lurkers = Number(data.lurker_count) || 0;
  const notReady = lurkerNotReady(data);
  const block = el('div', { class: 'block' });
  if (!notReady) {
    // 名单时间是「最近一次通讯录刷新」的时间，不是页面打开的时间。
    const when = lurkerTime(data.as_of);
    block.append(el('div', {
      class: 'block-note',
      text: when === '—' ? '成员名单的更新时间未知。' : `成员名单更新于 ${when}（北京时间）。`,
    }));
  }
  block.append(el('div', { class: 'cards cards-2' }, [
    statCard('当前在群', `${num(total)} 人`, notReady ? '名单还没刷新' : '', 'receive'),
    statCard('从没发过言', `${num(lurkers)} 人`, notReady ? '名单还没刷新' : '', 'plugin'),
  ]));
  return block;
}

function lurkerTable(data) {
  const rows = (Array.isArray(data.lurkers) ? data.lurkers : []).map(row => [
    lurkerNameCell(row),
    row.wxid || '—',
    lurkerTime(row.first_seen),
  ]);
  // 有名单时才解释「首次记录时间」；空表只需要一句正面结论。
  const note = rows.length
    ? '「首次记录时间」是机器人第一次看到这个成员的时间，不是他真实入群的时间；名单按这个时间从早到晚排列。'
    : '';
  // roster_total 为 0（可能机器人已退群/被移出）时不能说「成员都发过言」。
  const empty = (Number(data.roster_total) || 0) === 0
    ? '这个群当前名单是 0 人，可能已经没有在群成员：等下一轮通讯录刷新后再看。'
    : '这个群没有潜水成员：当前在群的成员都发过言。';
  return tableBox('潜水成员', ['昵称', '成员 ID', '首次记录时间'], rows,
    empty, note);
}

function lurkerView(data, onRetry) {
  const notReady = lurkerNotReady(data);
  const box = el('div');
  if (notReady) {
    // 名单没刷新过 ≠ 群里没人：把「0 人」的误解和下一步动作说清楚。
    box.append(alertBox('warn', 'alert',
      '成员名单还没有刷新过',
      '现在显示 0 人并不代表群里没人。请确认机器人连着微信、并且微信名同步正在运行，等它完成一轮通讯录刷新后再回来查看。',
      onRetry));
  }
  box.append(lurkerSummary(data));
  // 名单没刷新时没有可展示的成员，不摆一张「没有潜水成员」的空表，避免和真的没人潜水混在一起。
  if (!notReady) box.append(lurkerTable(data));
  return box;
}

async function renderLurkers(container) {
  const token = state.routeToken;
  // 群列表复用概览页「各群」的统计接口（它带群名）；潜水名单本身不带群名。
  // configured=1 让后端只返回 members 配置过的群：潜水接口不认别的群，选了必 400。
  const [groupResult, first] = await Promise.all([
    api('/api/stats/groups?limit=1000&configured=1'),
    api('/api/members/lurkers'),
  ]);
  if (token !== state.routeToken) return;

  const groupRows = Array.isArray(groupResult.data) ? groupResult.data : [];
  const currentChat = first.status === 200 && first.data && first.data.chat_id
    ? String(first.data.chat_id) : '';
  const groups = groupRows
    .filter(row => row && row.chat_id)
    .map(row => ({ chat_id: String(row.chat_id), chat_name: row.chat_name || '' }));
  // 默认群可能还没有任何消息记录，不在「各群」统计里；补一项，保证下拉框显示的就是当前群。
  if (currentChat && !groups.some(row => row.chat_id === currentChat)) {
    groups.push({ chat_id: currentChat, chat_name: '' });
  }

  const box = el('div');
  box.append(pageTitle('潜水名单',
    '这里列出当前还在群里、但从没发过言的成员。名单来自微信通讯录刷新：机器人的微信名同步需要保持运行，名单才会更新。'));

  const content = el('div');
  let select = null;
  let loadSeq = 0;

  function apply(result) {
    if (result.status !== 200) {
      content.replaceChildren(errorBlock(result, '潜水名单读取失败',
        () => { void reload(select ? select.value : currentChat); }));
      return;
    }
    content.replaceChildren(lurkerView(result.data || {},
      () => { void reload(select ? select.value : currentChat); }));
  }

  async function reload(chatId) {
    const seq = ++loadSeq;
    content.replaceChildren(loadingBox('正在读取潜水名单…', 6));
    const url = chatId
      ? '/api/members/lurkers?chat_id=' + encodeURIComponent(chatId)
      : '/api/members/lurkers';
    const result = await api(url);
    if (token !== state.routeToken || seq !== loadSeq) return;
    apply(result);
  }

  if (groups.length) {
    select = el('select', { class: 'input', id: 'lurker-chat' });
    for (const row of groups) {
      const name = String(row.chat_name || '').trim();
      // 有群名就显示群名，没有才退回 chat_id；完整 id 留在悬浮提示里，和「各群」表的写法一致。
      select.append(el('option', { value: row.chat_id, text: name || row.chat_id, title: row.chat_id }));
    }
    select.value = currentChat || groups[0].chat_id;
    select.addEventListener('change', () => { void reload(select.value); });
    box.append(el('div', { class: 'filters lurker-filters' }, [
      el('div', { class: 'filter-field' }, [
        el('label', { for: select.id, text: '群' }),
        select,
      ]),
    ]));
    if (groupResult.status !== 200) {
      box.append(el('div', {
        class: 'block-note',
        text: '群列表读取失败：' + errorText(groupResult, '未知原因') + '。现在只能查看默认群。',
      }));
    }
  }

  box.append(content);
  container.replaceChildren(box);
  apply(first);
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
  let extra = '';
  if (field.type === 'password') extra = '留空表示不修改';
  else if (field.type === 'map' || field.type === 'json') extra = 'JSON 格式';
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

  // 启停与保存成功后都要重拉插件列表、重建导航并刷新状态提示。
  async function refreshPluginState() {
    await loadPlugins(true);
    buildNav(currentRoute());
    const fresh = findPlugin(name);
    stateHint.textContent = fresh ? `状态：${fresh.state || '—'}` : '状态：—';
  }

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
    await refreshPluginState();
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
    await refreshPluginState();
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
    let frameSrc = url;
    if (state.token) {
      const sep = url.includes('?') ? '&' : '?';
      frameSrc = url + sep + 'token=' + encodeURIComponent(state.token);
    }
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
    // 请求期间用户可能切换插件：目标插件只在发请求前取一次，请求体与自动重载都用它。
    const target = pluginSelect.value;
    const response = await api('/api/debug', {
      body: {
        plugin: target,
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

// 拼查询串：空值（''/null/undefined）不写进 URL，读起来干净。
function apiUrl(path, params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === '' || value === null || value === undefined) continue;
    search.set(key, String(value));
  }
  const query = search.toString();
  return path + (query ? '?' + query : '');
}

/* ---------- 日志：按消息视图 ---------- */

// 会话标识优先用后端的显示名（群名 / 对方昵称）；拿不到（空串）时退回「群 id / 私聊 id」的写法。
function chatLabelOf(row) {
  const name = String(row.chat_name || '').trim();
  if (name) return name;
  const id = row.chat_id || '';
  if (!id) return row.unlinked ? '无关联' : '—';
  if (id.endsWith('@chatroom')) return '群 ' + id.slice(0, -'@chatroom'.length);
  return (row.is_group ? '群 ' : '私聊 ') + id;
}

// 会话列的悬浮提示：有显示名时连 id 一起给，没有就只给 id。
function chatTitleOf(row) {
  return [String(row.chat_name || '').trim(), row.chat_id || ''].filter(Boolean).join(' · ');
}

// ms 缺省显示成「—」而不是 0.0 ms：`fmtMs(null)` 会算出 0（Number(null) === 0），这里先挡一道。
function fmtItemMs(value) {
  return value === null || value === undefined || value === '' ? '—' : fmtMs(value);
}

// 折叠行的回复预览：换行先压成空格（行内不该出现空洞），超过上限的字数截断加省略号。
function replyPeek(text) {
  const flat = String(text || '').replace(/\s+/g, ' ').trim();
  return flat.length > REPLY_PEEK_CHARS ? flat.slice(0, REPLY_PEEK_CHARS) + '…' : flat;
}

// 明细归并：plugin / error 是插件这一次处理的记录，send / preview 是它的回复。
// 同一个插件的「处理」与「异常」各占一行：异常行紧跟在该插件的处理行之后，不同插件之间仍按处理顺序；
// 同一插件的多条处理记录依旧合成一行。其它类型（历史行里的 receive / system、没有插件名的发送）各占一行。
function messageEntries(items) {
  const entries = [];
  const handled = new Map();    // 插件名 → 处理行（不带异常），它的回复挂在这里
  const errorFirst = new Map(); // 插件名 → 第一条异常行：处理行要排在它前面
  const errorLast = new Map();  // 插件名 → 最后一条异常行：下一条异常排它后面
  const asRecord = item => entries.push({
    type: 'record', kind: item.kind, plugin: item.plugin || '',
    ms: item.ms, summary: item.summary || '', ts_utc: item.ts_utc,
  });

  for (const item of items) {
    const name = item.plugin || '';
    if (item.kind === 'plugin') {
      let entry = handled.get(name);
      if (!entry) {
        entry = {
          type: 'plugin', plugin: name, ms: null,
          summary: '', reply: '', replyKind: '', ts_utc: item.ts_utc,
        };
        handled.set(name, entry);
        const before = errorFirst.get(name);
        // 异常先到（少见）时把处理行插在它前面，两者紧挨着、处理行在上。
        if (before) entries.splice(entries.indexOf(before), 0, entry);
        else entries.push(entry);
      }
      if (!entry.summary) entry.summary = item.summary || '';
      if (entry.ms === null && item.ms !== null && item.ms !== undefined) entry.ms = item.ms;
      if (!entry.reply && item.reply) entry.reply = item.reply;
      continue;
    }
    if (item.kind === 'error') {
      const entry = {
        type: 'error', plugin: name, ms: item.ms,
        error: item.summary || '', reply: item.reply || '', ts_utc: item.ts_utc,
      };
      const anchor = errorLast.get(name) || handled.get(name);
      if (anchor) entries.splice(entries.indexOf(anchor) + 1, 0, entry);
      else entries.push(entry);
      errorLast.set(name, entry);
      if (!errorFirst.has(name)) errorFirst.set(name, entry);
      continue;
    }
    if (item.kind === 'send' || item.kind === 'preview') {
      // 没有插件名的发送记录不往插件行上挂，自己占一行（与原来的口径一致）。
      const entry = name ? (handled.get(name) || errorLast.get(name)) : null;
      if (!entry) {
        asRecord(item);
        continue;
      }
      if (!entry.reply) entry.reply = item.summary || '';
      // 一个插件既有预览又有发出时以「已回复」为准。
      if (entry.replyKind !== 'send') entry.replyKind = item.kind;
      continue;
    }
    asRecord(item);
  }
  return entries;
}

// 状态沿用插件表那套 .state + st-* 配色：已回复绿、已预览橙、未回复灰、异常红。
function entryStatus(entry) {
  if (entry.type === 'error') return { label: '异常', cls: 'st-bad' };
  if (entry.replyKind === 'preview') return { label: '已预览', cls: 'st-idle' };
  if (entry.replyKind === 'send' || entry.reply) return { label: '已回复', cls: 'st-run' };
  return { label: '未回复', cls: 'st-off' };
}

// 后端还没给 elapsed_ms 时的兼容口径：这条消息下各插件处理耗时之和；一个耗时都没有时返回 null。
function totalMs(entries) {
  let total = null;
  for (const entry of entries) {
    if (entry.type === 'record') continue;
    if (entry.ms === null || entry.ms === undefined) continue;
    const ms = Number(entry.ms);
    if (Number.isFinite(ms)) total = (total || 0) + ms;
  }
  return total;
}

// 行内耗时：优先用后端的 elapsed_ms（这条消息从收到到回复发出的实际耗时）。
// null 表示没有回复 —— 不给假数字，明说「未回复」；字段还没提供（undefined）时退回旧口径。
function elapsedTag(row, entries) {
  const value = row.elapsed_ms;
  if (value === undefined || value === '') {
    const total = totalMs(entries);
    return total === null ? null : el('span', { class: 'msg-tag', text: `总耗时 ${fmtMs(total)}` });
  }
  if (value === null) return el('span', { class: 'msg-tag mute', text: '未回复' });
  return el('span', { class: 'msg-tag', text: `回复耗时 ${fmtMs(value)}` });
}

// 回复正文：短的直接铺开，长的折起来（文案用 textContent 渲染，不拼 HTML）。
function replyNode(text) {
  const body = el('div', { class: 'reply-box', text });
  if (text.length <= 120) return body;
  return el('details', { class: 'msg-reply' }, [
    el('summary', { text: `展开回复（共 ${text.length} 字）` }),
    body,
  ]);
}

function entryNode(entry) {
  if (entry.type === 'record') {
    const kind = kindOf(entry.kind);
    return el('div', { class: 'msg-item' }, [
      el('span', { class: 'msg-item-kind ' + kind.cls, text: kind.short }),
      el('span', { class: 'msg-item-name', text: entry.plugin || '框架' }),
      el('span', { class: 'msg-item-ms', text: '耗时 ' + fmtItemMs(entry.ms) }),
      entry.summary ? el('span', { class: 'msg-item-sum', text: entry.summary }) : null,
    ]);
  }

  const status = entryStatus(entry);
  const isError = entry.type === 'error';
  const node = el('div', { class: 'msg-item ' + (isError ? 'is-error' : 'is-plugin') }, [
    el('div', { class: 'msg-item-head' }, [
      el('span', { class: 'msg-item-name', text: entry.plugin || '（没有插件名）' }),
      el('span', { class: 'state ' + status.cls, text: status.label }),
      el('span', { class: 'msg-item-ms', text: '耗时 ' + fmtItemMs(entry.ms) }),
    ]),
  ]);
  if (isError) {
    node.append(el('div', { class: 'msg-item-err', text: entry.error || '（没有错误摘要）' }));
  } else if (entry.summary) {
    node.append(el('div', { class: 'msg-item-sum', text: entry.summary }));
  }
  if (entry.reply) node.append(replyNode(entry.reply));
  return node;
}

// 一条消息的内容指纹：只用来判断「和上次渲染的一不一样」，不做别的。
function messageSignature(row) {
  const items = Array.isArray(row.items) ? row.items : [];
  return [
    row.msg_key, row.msg_id, row.ts_utc, row.chat_id, row.sender_id,
    row.chat_name || '', row.elapsed_ms,
    row.plugin_count, row.has_error ? 1 : 0, row.replied ? 1 : 0, row.reply_kind,
    (row.reply || '').length, (row.text || '').length, row.items_truncated ? 1 : 0,
    items.length, items.map(item => item.id).join(','),
  ].join('~');
}

// 一条消息一行：时间 / 群 / 发送者 / 正文 + 处理概要，展开是该消息的明细。
// 展开状态按消息标识记在 openKeys 里：跟随最新的轮询会重建列表，展开的行不能自己收起来。
function messageRow(row, openKeys) {
  const key = row.msg_key || 'unlinked';
  const entries = messageEntries(Array.isArray(row.items) ? row.items : []);
  const elapsed = elapsedTag(row, entries);
  const hasError = row.has_error === true || entries.some(entry => entry.type === 'error');
  const peek = row.replied && row.reply ? replyPeek(row.reply) : '';

  const pluginCount = Number(row.plugin_count) || 0;
  const tags = [];
  if (row.unlinked) tags.push(el('span', { class: 'msg-tag mute', text: '无关联记录' }));
  if (pluginCount) tags.push(el('span', { class: 'msg-tag', text: `处理 ${num(pluginCount)} 个插件` }));
  if (hasError) tags.push(el('span', { class: 'msg-tag bad', text: '异常' }));
  if (row.replied) {
    tags.push(el('span', { class: 'msg-tag ok', text: row.reply_kind === 'preview' ? '已预览' : '已回复' }));
  }
  if (elapsed) tags.push(elapsed);

  const summary = el('summary', { class: 'msg-sum' }, [
    icon('caret', 'msg-caret'),
    el('span', { class: 'msg-time', text: fmtShortTime(row.ts_utc) }),
    el('span', { class: 'msg-chat', text: chatLabelOf(row), title: chatTitleOf(row) || null }),
    el('span', { class: 'msg-sender', text: row.sender_id || '—', title: row.sender_id || '' }),
    el('span', { class: 'msg-body', text: row.text || '（没有正文）', title: row.text || '' }),
    peek ? el('span', { class: 'msg-peek', title: row.reply }, [
      el('span', { class: 'msg-peek-label', text: '回复' }),
      el('span', { class: 'msg-peek-text', text: peek }),
    ]) : null,
    tags.length ? el('span', { class: 'msg-tags' }, tags) : null,
  ]);

  const detail = el('div', { class: 'msg-detail' });
  if (row.unlinked) {
    detail.append(el('div', { class: 'msg-note', text: '这条记录没有关联到具体消息（历史数据或定时任务产生的记录），下面把它这一组记录全部列出。' }));
  }
  if (entries.length) for (const entry of entries) detail.append(entryNode(entry));
  else detail.append(el('div', { class: 'msg-note', text: '这条消息没有被插件处理的记录。' }));
  if (row.items_truncated) {
    detail.append(el('div', { class: 'msg-note', text: '这条消息的明细超过 200 条，这里只显示前 200 条。' }));
  }

  const node = el('details', {
    class: 'msg-row' + (hasError ? ' has-error' : '') + (row.replied ? ' has-reply' : ''),
  });
  node.append(summary, detail);
  if (openKeys.has(key)) node.open = true;
  node.addEventListener('toggle', () => {
    if (node.open) openKeys.add(key);
    else openKeys.delete(key);
  });
  return node;
}

async function renderLogs(container) {
  const token = state.routeToken;
  // 需要留在状态里的只有视图与两套类型筛选；其余筛选值直接从各自的输入控件读。
  const filters = { view: 'messages', msgKinds: '', recordsKind: '' };
  const view = { offset: 0, total: 0, following: true, limit: LOG_PAGE_SIZE };
  // 展开过的消息（按 msg_key 记）：跟随最新的轮询会重建列表，展开的行不能自己收起来。
  const openKeys = new Set();

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
  const directCheck = el('input', { type: 'checkbox', class: 'input-check', id: 'log-direct' });
  const systemCheck = el('input', { type: 'checkbox', class: 'input-check', id: 'log-system' });

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

  // 视图切换：「按消息」是默认视图，「按记录」保留原来的扁平行流与类型筛选。
  const viewNote = el('span', { class: 'logs-view-note' });
  const viewTabs = el('div', { class: 'logs-views', role: 'group', 'aria-label': '日志视图' });
  const viewTabButtons = [];
  for (const item of LOG_VIEWS) {
    const button = el('button', { class: 'logs-view-tab', type: 'button', text: item.label });
    button.addEventListener('click', () => switchView(item.value));
    viewTabButtons.push({ item, button });
    viewTabs.append(button);
  }
  viewTabs.append(viewNote);

  // 按消息视图的快捷筛选 + 两个开关：缺省只看群消息，「含私聊」只管私聊，
  // 「含系统记录」把框架自己那些没有会话的记录也带出来；两个开关互不影响。
  const facetsBar = el('div', { class: 'logs-facets', role: 'group', 'aria-label': '按消息筛选' });
  const facetButtons = [];
  for (const facet of MSG_FACETS) {
    const button = el('button', { class: 'facet', type: 'button', text: facet.label });
    button.addEventListener('click', () => {
      filters.msgKinds = facet.value;
      view.offset = 0;
      syncFacets();
      userRefresh();
    });
    facetButtons.push({ facet, button });
    facetsBar.append(button);
  }
  facetsBar.append(
    el('label', { class: 'switch' }, [directCheck, el('span', { text: '含私聊' })]),
    el('label', { class: 'switch' }, [systemCheck, el('span', { text: '含系统记录' })]),
  );

  const layout = el('div', { class: 'logs-layout' }, [
    kindsPanel,
    el('div', { class: 'logs-main' }, [
      filtersBar,
      facetsBar,
      stream,
      el('div', { class: 'pager' }, [prevBtn, nextBtn, pageInfo]),
    ]),
  ]);
  stream.append(loadingBox('正在读取日志…', 6));
  container.replaceChildren(pageTitle('日志', '一条消息在框架里走过的每一步；类型底色与左侧条数对应。'));
  container.append(viewTabs, layout);

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
        'aria-pressed': kind.value === filters.recordsKind ? 'true' : 'false',
      }, [el('span', { text: kind.label }), count]);
      item.addEventListener('click', () => {
        filters.recordsKind = kind.value;
        view.offset = 0;
        syncKinds(null, null);
        userRefresh();
      });
      kindButtons.push({ kind, item, count });
      kindsPanel.append(item);
    }
    kindsPanel.append(kindsNote);
  }

  // 就地更新条数与选中态：不重建 DOM，键盘焦点不会被「跟随最新」的定时刷新吃掉。
  function syncKinds(counts, errorResult) {
    for (const entry of kindButtons) {
      entry.item.setAttribute('aria-pressed', entry.kind.value === filters.recordsKind ? 'true' : 'false');
      if (counts) entry.count.textContent = num(counts[entry.kind.label] || 0);
      else if (errorResult) entry.count.textContent = '—';
    }
    if (errorResult) {
      kindsNote.hidden = !!counts;
      if (!counts) kindsNote.textContent = '条数读取失败：' + errorText(errorResult, '');
    }
  }

  function syncFacets() {
    for (const entry of facetButtons) {
      entry.button.setAttribute('aria-pressed', entry.facet.value === filters.msgKinds ? 'true' : 'false');
    }
  }

  // 切视图：筛选项各归各的（按消息有快捷筛选，按记录有类型条数），所以只重置翻页位置。
  function switchView(next) {
    if (filters.view === next) return;
    filters.view = next;
    view.offset = 0;
    lastSignature = '';
    applyView();
    stream.replaceChildren(loadingBox('正在读取日志…', 6));
    userRefresh();
  }

  function applyView() {
    const records = filters.view === 'records';
    layout.setAttribute('data-view', filters.view);
    facetsBar.hidden = records;
    for (const entry of viewTabButtons) {
      entry.button.setAttribute('aria-pressed', entry.item.value === filters.view ? 'true' : 'false');
    }
    const current = LOG_VIEWS.find(item => item.value === filters.view);
    viewNote.textContent = current ? current.note : '';
    syncFacets();
    updatePager();
  }

  function updatePager() {
    const unit = filters.view === 'messages' ? '条消息' : '条';
    const pages = Math.max(1, Math.ceil(view.total / view.limit));
    const current = Math.floor(view.offset / view.limit) + 1;
    pageInfo.textContent = `共 ${num(view.total)} ${unit}，第 ${current} / ${pages} 页` + (view.following ? '（跟随最新中，翻页已暂停）' : '');
    prevBtn.disabled = view.following || view.offset <= 0;
    nextBtn.disabled = view.following || view.offset + view.limit >= view.total;
  }

  /* ---------- 刷新调度 ----------
     一次请求在飞时怎么处理后来的触发，分两条路：
     - 轮询（3 秒一次）：直接跳过这一轮，不排队 —— 响应慢时不积压。
     - 用户主动操作（筛选 / 翻页 / 重试 / 切类型）：记一个 pending 标记，
       由 drain 的循环在当前请求跑完后立刻补跑一次，用户不会「点了没反应」。
     pending 用布尔而不是队列：补跑读的是控件里的当前值，多次操作合并成一次就够。 */
  let refreshing = false;
  let pending = false;
  // 上一次「按消息」渲染的内容指纹：一样就不重建 DOM。
  let lastSignature = '';

  // 筛选条里各控件的当前值（两个视图共用）。
  function filterParams() {
    return {
      keyword: searchInput.value.trim(),
      chat_id: chatInput.value.trim(),
      plugin: pluginSelect.value,
      since: toUtcIso(sinceInput.value),
      until: toUtcIso(untilInput.value),
    };
  }

  function failStream(result, fallback) {
    // 鉴权失败时跟随轮询没有意义：只会每 3 秒重发一次注定 401 的请求
    if (result && result.status === 401) {
      view.following = false;
      followCheck.checked = false;
      stopFollow();
    }
    stream.innerHTML = '';
    stream.append(errorBlock(result, fallback, () => userRefresh()));
    pageInfo.textContent = '';
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    lastSignature = '';   // 出错时清指纹：恢复后必须重画，不能拿指纹跳过
  }

  // 真正取一次数并渲染（不含并发策略）。
  async function loadOnce() {
    const base = filterParams();

    if (filters.view === 'messages') {
      const result = await api(apiUrl('/api/logs/messages', Object.assign({}, base, {
        kinds: filters.msgKinds,
        // 缺省就不带私聊 / 系统记录；只在开关打开时才带上参数，URL 保持干净。
        include_direct: directCheck.checked ? 1 : '',
        include_system: systemCheck.checked ? 1 : '',
        limit: view.limit,
        offset: view.offset,
      })));
      if (token !== state.routeToken) return;   // 路由已经切走，别把旧数据画上去
      if (result.status !== 200) {
        failStream(result, '消息日志读取失败');
        return;
      }
      view.total = Number(result.data.total) || 0;
      const rows = Array.isArray(result.data.rows) ? result.data.rows : [];
      // 内容没变就不动 DOM：3 秒一次的轮询不该把用户展开的行、正要点的行重建一遍。
      const signature = ['messages', filters.msgKinds, directCheck.checked ? 1 : 0,
        systemCheck.checked ? 1 : 0, view.offset, view.total]
        .join('@') + '|' + rows.map(messageSignature).join('||');
      if (signature === lastSignature) {
        updatePager();
        return;
      }
      lastSignature = signature;
      stream.innerHTML = '';
      if (!rows.length) {
        // 空结果时把「还能打开什么」说清楚：只说当前确实关着的开关。
        const hints = [];
        if (!directCheck.checked) hints.push('私聊用「含私聊」打开');
        if (!systemCheck.checked) hints.push('框架自己的记录用「含系统记录」打开');
        stream.append(emptyBox(hints.length
          ? `没有符合条件的消息；${hints.join('，')}。`
          : '没有符合条件的消息。'));
      } else for (const row of rows) stream.append(messageRow(row, openKeys));
      updatePager();
      return;
    }

    const page = await api(apiUrl('/api/logs', Object.assign({}, base, {
      kinds: filters.recordsKind,
      limit: view.limit,
      offset: view.offset,
    })));
    if (token !== state.routeToken) return;   // 路由已经切走，别把旧数据画上去
    if (page.status !== 200) {
      failStream(page, '日志读取失败');
      return;
    }
    view.total = Number(page.data.total) || 0;
    const rows = Array.isArray(page.data.rows) ? page.data.rows : [];
    // 与「按消息」一致：内容没变就不重建 DOM，也不丢用户正在选中的文本
    const signature = ['records', filters.recordsKind, view.offset, view.total].join('@')
      + '|' + rows.map(row => row.id).join(',');
    if (signature === lastSignature) {
      updatePager();
      return;
    }
    lastSignature = signature;
    // 分类计数是 7 条全表 COUNT：只在内容真的变了时刷新，不必每 3 秒跑一次。
    const counts = await api('/api/logs/counts');
    if (token !== state.routeToken) return;
    syncKinds(counts.status === 200 && counts.data && counts.data.counts ? counts.data.counts : null, counts);
    stream.innerHTML = '';
    if (!rows.length) stream.append(emptyBox('没有符合条件的记录。'));
    else for (const row of rows) stream.append(logRow(row));
    updatePager();
  }

  // 调度循环：一次只跑一个请求；跑完发现还有 pending 就再跑一次。
  // 用 while 循环而不是递归调用自己，栈不会随补跑次数增长。
  async function drain() {
    try {
      refreshing = true;
      do {
        pending = false;      // 必须在补跑之前清掉，否则同一件事会被反复补跑
        await loadOnce();
      } while (pending && token === state.routeToken);
    } finally {
      refreshing = false;     // 成功 / 失败 / 抛错三条路都要复位
    }
  }

  // 轮询入口：正在刷新就跳过这一轮（不记 pending，避免积压）。
  function pollRefresh() {
    if (refreshing) return;
    void drain();
  }

  // 用户主动入口：正在刷新就记待补跑，当前请求结束后补一次。
  function userRefresh() {
    if (refreshing) {
      pending = true;
      return;
    }
    void drain();
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
        pollRefresh();
      }, FOLLOW_MS);
    }
    updatePager();
  }

  let searchTimer = null;
  searchInput.addEventListener('input', () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      view.offset = 0;
      userRefresh();
    }, 300);
  });
  chatInput.addEventListener('change', () => { view.offset = 0; userRefresh(); });
  pluginSelect.addEventListener('change', () => { view.offset = 0; userRefresh(); });
  sinceInput.addEventListener('change', () => { view.offset = 0; userRefresh(); });
  untilInput.addEventListener('change', () => { view.offset = 0; userRefresh(); });
  directCheck.addEventListener('change', () => { view.offset = 0; userRefresh(); });
  systemCheck.addEventListener('change', () => { view.offset = 0; userRefresh(); });
  followCheck.addEventListener('change', () => {
    view.following = followCheck.checked;
    applyFollowing();
    userRefresh();
  });
  prevBtn.addEventListener('click', () => {
    view.offset = Math.max(0, view.offset - view.limit);
    userRefresh();
  });
  nextBtn.addEventListener('click', () => {
    view.offset = view.offset + view.limit;
    userRefresh();
  });

  buildKinds();
  applyView();
  await drain();
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
