// 运行时后端地址配置（会被 index.html 在加载主包前同步执行）
// 同域部署（后端 serve_frontend 托管前端）无需设置，getApiBaseUrl() 走 /api。
// 静态托管（GitHub Pages / Vercel / Netlify / Cloudflare Pages 等没有后端）下，
// 必须显式把 API 指向后端，否则 /api/* 会打到静态主机并返回 405。
(function () {
  try {
    var host = window.location.hostname;
    var isStatic =
      /(^|\.)github\.io$/.test(host) ||
      /(^|\.)vercel\.app$/.test(host) ||
      /(^|\.)netlify\.app$/.test(host) ||
      /(^|\.)pages\.dev$/.test(host) ||
      /(^|\.)workers\.dev$/.test(host);
    if (isStatic) {
      window.__APP_ENV__ = window.__APP_ENV__ || {};
      window.__APP_ENV__.VITE_API_URL = 'https://fanshu-writer-backend.onrender.com/api';
    }
  } catch (e) {}
})();