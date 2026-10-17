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

const STATE_CLASS = { '运行中': 'st-run', '已停用': 'st-off', '未运行': 'st-idle', '已降级': 'st-bad' };

const TYPE_LABEL = {
  text: '文本', password: '密码', int: '整数', float: '小数',
  bool: '开关', enum: '枚举', map: '键值表', json: '原始 JSON',
};

const state = {
  plugins: { rows: [], day: '', error: '', at: 0 },
  routeToken: 0,
  followTimer: null,
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

function errorBlock(result, fallback) {
  const detail = backendText(result);
  const hint = statusHint(result ? result.status : 0);
  if (hint && detail) {
    return el('div', { class: 'alert alert-error' }, [
      el('div', { text: hint }),
      el('div', { class: 'detail', text: '后端原文：' + detail }),
    ]);
  }
  return el('div', { class: 'alert alert-error', text: hint || detail || fallback || '请求失败' });
}

function errorBox(text) { return el('div', { class: 'alert alert-error', text }); }
function emptyBox(text) { return el('div', { class: 'empty', text }); }

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
  const head = el('tr', null, headers.map(header => el('th', { text: header })));
  const body = el('tbody', null, rows.map(cells => el('tr', null, cells.map(cell => {
    if (cell instanceof Node) return el('td', null, cell);
    const text = cell === null || cell === undefined || cell === '' ? '—' : String(cell);
    return el('td', { text });
  }))));
  box.append(el('table', { class: 'grid' }, [el('thead', null, head), body]));
  return box;
}

/* ---------- ❓ 浮窗 ---------- */

let popoverNode = null;

function closePopover() {
  if (popoverNode) {
    popoverNode.remove();
    popoverNode = null;
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
  closePopover();
  const help = field.help && String(field.help).trim() ? String(field.help) : '插件没有为这一项写说明。';
  const rows = [
    ['字段', field.key || ''],
    ['类型', TYPE_LABEL[field.type] || field.type || '未知'],
    ['默认值', defaultText(field)],
    ['取值范围', rangeText(field)],
  ];
  const panel = el('div', { class: 'popover' }, [
    el('div', { class: 'popover-title', text: field.label || field.key || '字段说明' }),
    ...rows.map(([key, value]) => el('div', { class: 'popover-row' }, [
      el('span', { class: 'k', text: key }),
      el('span', { class: 'v', text: value }),
    ])),
    el('div', { class: 'popover-row' }, [
      el('span', { class: 'k', text: '说明' }),
      el('span', { class: 'v', text: help }),
    ]),
  ]);
  document.body.append(panel);
  const rect = anchor.getBoundingClientRect();
  const maxLeft = document.documentElement.clientWidth - panel.offsetWidth - 12;
  panel.style.top = (rect.bottom + 6) + 'px';
  panel.style.left = Math.max(8, Math.min(rect.left - 40, maxLeft)) + 'px';
  popoverNode = panel;
}

document.addEventListener('click', event => {
  if (!popoverNode) return;
  if (popoverNode.contains(event.target)) return;
  if (event.target.closest && event.target.closest('.help-btn')) return;
  closePopover();
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') closePopover();
});

/* ---------- 插件列表（导航与多个页面共用） ---------- */

async function loadPlugins(force) {
  const now = Date.now();
  if (!force && state.plugins.at && now - state.plugins.at < PLUGIN_CACHE_MS) return state.plugins;
  const result = await api('/api/plugins');
  if (result.status !== 200) {
    state.plugins = { rows: [], day: '', error: errorText(result, '插件列表读取失败'), at: Date.now() };
    return state.plugins;
  }
  state.plugins = {
    rows: Array.isArray(result.data.plugins) ? result.data.plugins : [],
    day: result.data.day || '',
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
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (parts[0] === 'plugins' && parts[1]) {
    if (parts[2] === 'page' && parts[3]) return { name: 'plugin-page', plugin: parts[1], key: parts[3] };
    return { name: 'plugin-config', plugin: parts[1] };
  }
  if (parts[0] === 'debug') return { name: 'debug' };
  if (parts[0] === 'logs') return { name: 'logs' };
  return { name: 'overview' };
}

function navLink(label, href, active, extraClass) {
  return el('a', {
    class: 'nav-link' + (extraClass ? ' ' + extraClass : '') + (active ? ' active' : ''),
    href,
    text: label,
    title: label,
  });
}

function buildNav(target) {
  const nav = document.getElementById('nav-tree');
  nav.innerHTML = '';
  nav.append(navLink('概览', '#/overview', target.name === 'overview'));
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

async function route() {
  const token = ++state.routeToken;
  stopFollow();
  closePopover();
  const target = currentRoute();
  await loadPlugins(false);
  if (token !== state.routeToken) return;
  buildNav(target);
  const main = document.getElementById('main');
  main.innerHTML = '';
  if (target.name === 'plugin-config') return renderPluginConfig(main, target.plugin);
  if (target.name === 'plugin-page') return renderPluginPage(main, target.plugin, target.key);
  if (target.name === 'debug') return renderDebug(main);
  if (target.name === 'logs') return renderLogs(main);
  return renderOverview(main);
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
  const scale = max > 0 ? 54 / max : 0;
  const chart = el('div', { class: 'trend' });
  rows.forEach((row, index) => {
    const received = Number(row.received) || 0;
    const sent = Number(row.sent) || 0;
    chart.append(el('div', { class: 'trend-day' }, [
      el('div', { class: 'trend-bars', title: `${row.day}　收到 ${received} · 发出 ${sent}` }, [
        el('div', { class: 'trend-receive', style: { height: Math.round(received * scale) + 'px' } }),
        el('div', { class: 'trend-send', style: { height: Math.round(sent * scale) + 'px' } }),
      ]),
      el('div', { class: 'trend-label', text: trendLabel(row.day, index === rows.length - 1) }),
    ]));
  });
  panel.append(chart);
  panel.append(el('div', { class: 'legend' }, [
    el('span', { class: 'receive', text: '■ 收到' }),
    document.createTextNode('　'),
    el('span', { class: 'send', text: '■ 发出' }),
  ]));
  if (max === 0) panel.append(el('div', { class: 'block-note', text: '这 7 天还没有记录到收发消息。' }));
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
  if (state.plugins.error) return errorBox(state.plugins.error);
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
  container.append(box);
}

/* ---------- 插件配置 ---------- */

function jsonText(value) {
  try {
    return JSON.stringify(value === undefined ? null : value, null, 2);
  } catch (error) {
    return '';
  }
}

function buildFieldInput(field) {
  const type = field.type;
  if (type === 'bool') {
    const input = el('input', { type: 'checkbox', class: 'input-check' });
    input.checked = field.value === true;
    return input;
  }
  if (type === 'enum') {
    const select = el('select', { class: 'input' });
    const choices = Array.isArray(field.choices) ? field.choices : [];
    // 每个 option 的原始 choice 值按索引记下来：后端按严格类型比较，
    // 数字/布尔选项不能一律当字符串交回去。
    const original = [];
    const current = field.value === null || field.value === undefined ? '' : String(field.value);
    if (current === '') {
      // 当前值为空时给一个占位项，免得 select.value 落空、下拉框显示空白。
      select.append(el('option', { value: '', text: '（当前为空，未设置）' }));
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
    const input = el('input', { class: 'input', type: 'number', step: type === 'float' ? 'any' : '1' });
    input.value = field.value === null || field.value === undefined ? '' : String(field.value);
    return input;
  }
  if (type === 'map' || type === 'json') {
    const area = el('textarea', { class: 'input input-area', rows: '4', spellcheck: 'false' });
    area.value = jsonText(field.value);
    return area;
  }
  const input = el('input', { class: 'input', type: type === 'password' ? 'password' : 'text' });
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
  const helpBtn = el('button', { class: 'help-btn', type: 'button', text: '❓', title: '看这一项的说明与取值范围' });
  helpBtn.addEventListener('click', event => {
    event.preventDefault();
    openPopover(helpBtn, field);
  });
  const extra = field.type === 'password' ? '留空表示不修改'
    : (field.type === 'map' || field.type === 'json') ? 'JSON 格式'
      : '';
  return el('div', { class: 'field-row' }, [
    el('div', { class: 'field-label' }, [
      el('span', { class: 'field-name', text: field.label || field.key }),
      el('span', { class: 'field-key', text: field.key }),
      helpBtn,
    ]),
    el('div', { class: 'field-input' }, input),
    el('div', { class: 'field-extra', text: extra }),
  ]);
}

function degradedBanner(reason) {
  return el('div', { class: 'alert alert-warn' }, [
    el('div', { text: '插件已降级：它还在列表里，但加载或运行时报了错，配置可能没生效。' }),
    el('div', { class: 'detail', text: reason || '（没有给出原因）' }),
  ]);
}

function undeclaredView(data) {
  const box = el('div', { class: 'block' });
  box.append(el('div', { class: 'alert alert-info' }, [
    el('div', { text: '该插件没有在 plugin.json 里声明配置项，管理台不能改它。' }),
    el('div', { class: 'detail', text: '下面是 bot_config.json 里该插件的原始配置，只读；要改请直接改文件，改完用控制台的 reload 命令重载。' }),
  ]));
  const raw = data && data.raw && typeof data.raw === 'object' ? data.raw : {};
  box.append(el('h2', { class: 'block-title', text: '原始 JSON（只读）' }));
  box.append(el('pre', { class: 'raw-json', text: JSON.stringify(raw, null, 2) }));
  if (!Object.keys(raw).length) box.append(el('div', { class: 'block-note', text: '（该插件在 bot_config.json 里没有任何配置）' }));
  return box;
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
  const source = result.status !== 200
    ? '配置声明读不出来：下面是后端返回的错误原文。'
    : declared
      ? (data.source === 'bot_config'
        ? `配置来源：bot_config.json 的 plugins.${name}`
        : `配置来源：插件目录下的 ${data.file || '配置文件'}`)
      : '该插件没有声明配置项，只能看原始 JSON。';

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
    container.append(box);
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
    container.append(box);
    return;
  }

  const entries = [];
  const sections = Array.isArray(data.sections) ? data.sections : [];
  let maskedCount = 0;
  for (const section of sections) {
    box.append(el('h2', { class: 'section-title', text: section.title || '（未命名分区）' }));
    for (const field of Array.isArray(section.fields) ? section.fields : []) {
      if (field.masked) maskedCount += 1;
      const input = buildFieldInput(field);
      entries.push({ field, input });
      box.append(fieldRow(field, input));
    }
  }
  if (!entries.length) box.append(emptyBox('该插件声明了配置，但没有声明任何字段。'));
  if (maskedCount) box.append(el('div', { class: 'block-note', text: '密码类字段不回显明文，留空表示保持原值不变。' }));

  saveBtn.addEventListener('click', async () => {
    const values = {};
    for (const entry of entries) values[entry.field.key] = readFieldValue(entry.field, entry.input);
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

  container.append(box);
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
    box.append(errorBox('取不到插件页面（连不上管理台）：' + url));
  } else if (!probe.ok) {
    box.append(errorBox(`插件页面不存在（HTTP ${probe.status}）：${url}；请确认插件目录下 web/ 里有这个文件。`));
  } else {
    box.append(el('div', { class: 'block-note' }, [
      el('span', { text: '页面地址：' + url + '　' }),
      el('a', { href: url, target: '_blank', text: '在新标签页打开' }),
    ]));
    box.append(el('iframe', { class: 'plugin-frame', src: url, title: name + ' · ' + key }));
  }
  container.append(box);
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
  box.append(el('div', { class: 'alert alert-warn' }, [
    el('div', { text: '调试会真实调用外部服务（可能产生费用或触发限流），也会改动插件的内部状态。' }),
    el('div', { class: 'detail', text: '调试只走插件分发，不经过白名单、管理员拦截与人格切换，命中情况可能比真实运行时乐观；「真的发到微信」默认关闭，刷新页面后复位。' }),
  ]));

  const pluginSelect = el('select', { class: 'input' });
  pluginSelect.append(el('option', { value: '', text: '全部插件' }));
  for (const row of state.plugins.rows) pluginSelect.append(el('option', { value: row.name, text: row.name }));

  const chatInput = el('input', { class: 'input', type: 'text', placeholder: '例如 12345678@chatroom；私聊填 wxid_xxx' });
  const senderInput = el('input', { class: 'input', type: 'text', placeholder: '例如 wxid_xxx（可留空）' });
  const textInput = el('textarea', { class: 'input input-area', rows: '4', placeholder: '例如 /bga排行榜' });
  const realCheck = el('input', { type: 'checkbox' });
  const reloadCheck = el('input', { type: 'checkbox' });
  const runBtn = el('button', { class: 'btn primary', type: 'button', text: '跑一遍' });
  const formMsg = el('span', { class: 'inline-msg' });
  const resultBox = el('div', { class: 'debug-result' });

  const form = el('div', { class: 'debug-form' }, [
    el('div', { class: 'form-row' }, [el('label', { text: '插件' }), pluginSelect]),
    el('div', { class: 'form-row' }, [el('label', { text: '会话 ID' }), chatInput]),
    el('div', { class: 'form-row' }, [el('label', { text: '发送者' }), senderInput]),
    el('div', { class: 'form-row' }, [el('label', { text: '内容' }), textInput]),
    el('div', { class: 'debug-actions' }, [runBtn, formMsg]),
    el('div', { class: 'debug-switches' }, [
      el('label', { class: 'switch' }, [realCheck, el('span', { text: '真的发到微信' })]),
      el('label', { class: 'switch' }, [reloadCheck, el('span', { text: '跑完自动重载插件' })]),
    ]),
  ]);
  resultBox.append(emptyBox('填好左边，点「跑一遍」看结果。'));
  box.append(el('div', { class: 'debug-layout' }, [form, resultBox]));
  container.append(box);

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
    resultBox.append(emptyBox('正在跑一遍，插件可能真的访问外部服务，最长等 30 秒…'));
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
  const filters = { kind: '', keyword: '', chat_id: '', plugin: '', since: '', until: '' };
  const view = { offset: 0, total: 0, following: true, limit: LOG_PAGE_SIZE };

  const kindsPanel = el('div', { class: 'logs-kinds' });
  const stream = el('div', { class: 'log-stream' });
  const pageInfo = el('span');
  const prevBtn = el('button', { class: 'btn', type: 'button', text: '上一页' });
  const nextBtn = el('button', { class: 'btn', type: 'button', text: '下一页' });

  const searchInput = el('input', { class: 'input', type: 'text', placeholder: '搜内容 / 群 ID' });
  const chatInput = el('input', { class: 'input', type: 'text', placeholder: '群 ID（可留空）' });
  const pluginSelect = el('select', { class: 'input' });
  pluginSelect.append(el('option', { value: '', text: '全部插件' }));
  for (const row of state.plugins.rows) pluginSelect.append(el('option', { value: row.name, text: row.name }));
  const sinceInput = el('input', { class: 'input time', type: 'datetime-local', title: '起始时间（北京时间）' });
  const untilInput = el('input', { class: 'input time', type: 'datetime-local', title: '结束时间（北京时间）' });
  const followCheck = el('input', { type: 'checkbox' });
  followCheck.checked = true;

  const filtersBar = el('div', { class: 'filters' }, [
    searchInput,
    chatInput,
    pluginSelect,
    sinceInput,
    el('span', { class: 'hint', text: '至' }),
    untilInput,
    el('span', { class: 'hint', text: '（时间按北京时间）' }),
    el('label', { class: 'switch' }, [followCheck, el('span', { text: '跟随最新' })]),
  ]);

  const layout = el('div', { class: 'logs-layout' }, [
    kindsPanel,
    el('div', { class: 'logs-main' }, [
      filtersBar,
      stream,
      el('div', { class: 'pager' }, [prevBtn, nextBtn, pageInfo]),
    ]),
  ]);
  container.append(pageTitle('日志', '一条消息在框架里走过的每一步；类型底色与左侧条数对应。'));
  container.append(layout);

  function renderCounts(result) {
    const counts = result.status === 200 && result.data && result.data.counts ? result.data.counts : null;
    kindsPanel.innerHTML = '';
    for (const kind of LOG_KINDS) {
      const item = el('div', {
        class: 'log-kind-item' + (kind.value === filters.kind ? ' active' : '') + (kind.cls ? ' ' + kind.cls : ''),
      }, [
        el('span', { text: kind.label }),
        el('span', { class: 'count', text: counts ? num(counts[kind.label] || 0) : '—' }),
      ]);
      item.addEventListener('click', () => {
        filters.kind = kind.value;
        view.offset = 0;
        refresh();
      });
      kindsPanel.append(item);
    }
    if (!counts) kindsPanel.append(el('div', { class: 'nav-note', text: '条数读取失败：' + errorText(result, '') }));
  }

  function updatePager() {
    const pages = Math.max(1, Math.ceil(view.total / view.limit));
    const current = Math.floor(view.offset / view.limit) + 1;
    pageInfo.textContent = `共 ${num(view.total)} 条，第 ${current} / ${pages} 页` + (view.following ? '（跟随最新中，翻页已暂停）' : '');
    prevBtn.disabled = view.following || view.offset <= 0;
    nextBtn.disabled = view.following || view.offset + view.limit >= view.total;
  }

  async function refresh() {
    const page = await api(logsUrl({
      kinds: filters.kind,
      keyword: searchInput.value.trim(),
      chat_id: chatInput.value.trim(),
      plugin: pluginSelect.value,
      since: toUtcIso(sinceInput.value),
      until: toUtcIso(untilInput.value),
      limit: view.limit,
      offset: view.offset,
    }));
    if (token !== state.routeToken) return;
    const counts = await api('/api/logs/counts');
    if (token !== state.routeToken) return;
    renderCounts(counts);
    if (page.status !== 200) {
      stream.innerHTML = '';
      stream.append(errorBlock(page, '日志读取失败'));
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
route();
