import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { execSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, cpSync, writeFileSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { resolve, join, relative } from 'node:path'

// =======================================================================
// 「前后端对齐」防呆插件（根治 push 了但线上还是旧界面）
//
// 后端 Flask 按以下优先级读前端：
//   1) env APP_FRONTEND_DIST
//   2) backend/static/index.html + assets/  ← 生产默认
//   3) source/frontend/dist/                 ← 本地开发 fallback
//
// 本插件在 vite build 结束后自动做三件事：
//   A. 写 FRONTEND_DIST/version.json（commitId / builtAt / indexMd5）
//   B. 同步 dist/* → backend/static/
//   C. 打印 md5 对账
// =======================================================================
function buildAlignPlugin() {
  const ROOT = resolve(__dirname, '..')               // /workspace/source
  const BACKEND_STATIC = join(ROOT, 'backend', 'static')
  const GIT_ROOT = resolve(ROOT, '..')                // /workspace

  function md5File(p: string): string {
    const buf = readFileSync(p)
    return createHash('md5').update(buf).digest('hex')
  }
  function shortCommitId(): string {
    try {
      return execSync('git rev-parse --short HEAD', { cwd: GIT_ROOT, encoding: 'utf8' }).trim()
    } catch { return 'unknown' }
  }
  function fullCommitId(): string {
    try {
      return execSync('git rev-parse HEAD', { cwd: GIT_ROOT, encoding: 'utf8' }).trim()
    } catch { return 'unknown' }
  }

  return {
    name: 'build-align',
    closeBundle() {
      // 本插件只在 build（非 dev/preview）阶段执行
      // —— closeBundle 是 vite build 结束、dist 所有文件落盘后的钩子
      const distDir = resolve(__dirname, 'dist')
      const indexHtml = join(distDir, 'index.html')
      if (!existsSync(indexHtml)) return

      // ---------- A. 写 version.json 到 dist/ ----------
      const indexMd5 = md5File(indexHtml)
      const versionPayload = JSON.stringify({
        commit: shortCommitId(),
        commitFull: fullCommitId(),
        builtAt: new Date().toISOString(),
        indexMd5,
        indexSize: statSync(indexHtml).size,
      }, null, 2) + '\n'
      mkdirSync(join(distDir, '.fan-tmp'), { recursive: true })  // 占位，避免空文件夹难排查
      writeFileSync(join(distDir, 'version.json'), versionPayload, 'utf8')
      console.log('\n📘 [align] version.json 写入 dist/version.json:')
      console.log('   commit =', shortCommitId(), '  indexMd5 =', indexMd5)

      // ---------- B. cp -rf dist/* → backend/static/ ----------
      // 关键（v2·根治旧 JS 不消失）：
      // 同步前先删掉 BACKEND_STATIC/assets 里 所有带 hash 的旧构建产物（index-*.js/css, chunk-*.js/css, *.woff/ttf 字体文件），
      // 防止上一次构建的旧 JS/CSS 留在 static → GitHub/Rundle/Service Worker 还能命中旧版本，导致用户"刷新后还是旧界面"。
      // static 根目录下 logo.png/dian.jpg/favicon.svg 等 dist 中没有的资源不动。
      if (!existsSync(BACKEND_STATIC)) {
        mkdirSync(BACKEND_STATIC, { recursive: true })
      }
      const dstAssets = join(BACKEND_STATIC, 'assets')
      try {
        if (existsSync(dstAssets)) {
          // 用 find 删：只删带短横杠hash典型命名的资产（index-*.js, index-*.css, chunk-*.js, 字体KaTeX_*-hash.* 等）
          // 若未来用户放自定义 assets，文件不要用 "xxx-xxxxxx.ext"（横杠后跟一串hash）命名即可安全。
          execSync(
            `cd ${JSON.stringify(dstAssets)} && find . -maxdepth 1 -type f \\( -name 'index-*' -o -name 'chunk-*' -o -name 'KaTeX_*' -o -name '*-????????.*' \\) -delete 2>/dev/null || true`
          )
        }
      } catch { /* noop */ }
      const entries = ['index.html', 'assets', 'version.json']
      for (const name of entries) {
        const src = join(distDir, name)
        const dst = join(BACKEND_STATIC, name)
        if (!existsSync(src)) continue
        // 注意 cpSync(dereference+force) 在 node 16+ 稳定；force=true 覆盖旧哈希文件
        cpSync(src, dst, { recursive: true, force: true, dereference: true, errorOnExist: false })
      }
      // 清理占位目录（dist 里保留占位只是为了本地 dist 结构完整，static 里不需要）
      const dstTmp = join(BACKEND_STATIC, '.fan-tmp')
      try {
        if (existsSync(dstTmp)) execSync(`rm -rf ${JSON.stringify(dstTmp)}`)
      } catch { /* noop */ }

      // ---------- C. 哈希对账（失败直接抛错，阻止后续 commit 错版本） ----------
      const dstIndex = join(BACKEND_STATIC, 'index.html')
      const dstMd5 = md5File(dstIndex)
      if (dstMd5 !== indexMd5) {
        throw new Error(
          `🚨 [align] 前后端 index.html 哈希不一致！\n` +
          `   dist:   ${indexMd5}  (${relative(GIT_ROOT, indexHtml)})\n` +
          `   static: ${dstMd5}  (${relative(GIT_ROOT, dstIndex)})\n` +
          `   请手动 rm -rf source/backend/static/assets 后重新 npm run build`
        )
      }
      const dstVersion = join(BACKEND_STATIC, 'version.json')
      writeFileSync(dstVersion, versionPayload, 'utf8')
      console.log('✅ [align] dist → backend/static 同步完成，哈希一致:', indexMd5)
    },
  }
}

// 构建期清理 HTML：删除 HTML 模板中所有 <!-- xxx --> 注释（源码安全）
function htmlSanitizePlugin() {
  return {
    name: 'html-sanitize',
    transformIndexHtml(html: string): string {
      // 删除标准 HTML 注释（<!-- ... -->），不破坏 <script>/<style> 内内容
      return html.replace(/<!--[\s\S]*?-->/g, '');
    },
  };
}

export default defineConfig({
  base: './',
  plugins: [react(), buildAlignPlugin(), htmlSanitizePlugin()],
  build: {
    // 【加密】生产构建彻底关闭 sourcemap
    sourcemap: false,
    minify: 'terser',
    terserOptions: {
      // 进一步压缩：删掉所有 console.*（开发调试信息）和 debugger
      compress: { drop_console: true, drop_debugger: true, passes: 2 },
      mangle: { safari10: true },
      format: { comments: false },
    } as any,
    rollupOptions: {
      output: {
        // 去掉 chunk 文件名里的可读 vendor/app 前缀，仅保留 hash 降低逆向可识别性
        chunkFileNames: 'assets/[hash].js',
        assetFileNames: 'assets/[hash][extname]',
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:5000',
        changeOrigin: true,
      }
    },
    allowedHosts: ['.monkeycode-ai.online']
  }
})
