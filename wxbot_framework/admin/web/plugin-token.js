// 框架注入到插件页的脚本：给本页发出的同源 /api 请求自动补上 X-Admin-Token。
// 令牌只从本页网址的 token 查询参数里读：服务端不把令牌写进 HTML，也不写日志。
// 网址里没有 token 时完全惰性——请求照发，不加头，不报错。
// 只覆盖 window.fetch：插件页必须用 fetch，并在 DOMContentLoaded 之后取数；
// XHR 与解析期同步发出的请求不在补丁范围内，会因缺令牌被 401。
(function () {
  'use strict';

  const token = new URLSearchParams(window.location.search).get('token');
  if (!token) return;
  const nativeFetch = window.fetch;
  if (typeof nativeFetch !== 'function') return;

  // 只认同源、且路径以 /api/ 开头的地址：跨域或绝对地址不加头（加了也是给别的站点送凭证）。
  function isOwnApi(input) {
    const raw = typeof input === 'string' ? input : (input && (input.url || input.href)) || '';
    if (!raw) return false;
    let parsed;
    try {
      parsed = new URL(raw, window.location.href);
    } catch (error) {
      return false;
    }
    return parsed.origin === window.location.origin && parsed.pathname.startsWith('/api/');
  }

  window.fetch = function (input, init) {
    if (!isOwnApi(input)) return nativeFetch.call(this, input, init);
    try {
      // 复制 init 而不是改原对象：插件页若复用同一个 init，不能被这次补头污染。
      const options = Object.assign({}, init || {});
      // Request 对象自带的头要并进来，否则补头会把它原有的头整个替换掉。
      const headers = new Headers(options.headers || (input && input.headers) || undefined);
      // 已自带凭证时不覆盖；方法、正文与其它头一概不动。
      if (!headers.has('X-Admin-Token')) headers.set('X-Admin-Token', token);
      options.headers = headers;
      return nativeFetch.call(this, input, options);
    } catch (error) {
      // 补头失败就按原样发请求：注入脚本绝不能把插件页自己的取数弄坏。
      return nativeFetch.call(this, input, init);
    }
  };
})();
