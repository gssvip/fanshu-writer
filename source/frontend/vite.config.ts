import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { execSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, cpSync, writeFileSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { resolve, join, relative } from 'node:path'

// =======================================================================
// 「前后端对齐」防呆插件（根治 GitHub push 了但线上还是旧界面）
//
// Render rootDir = source/backend，Flask app.py 按这个优先级读前端：
//   1) env FANSHU_FRONTEND_DIST
//   2) backend/static/index.html + assets/  ← 生产默认（git 跟踪！）
//   3) source/frontend/dist/                 ← 本地开发 fallback
//
// 本插件在 vite build 结束后自动做 3 件事，以后你在任何地方跑
// `npm run build` / `npm run build-and-sync` / deploy.sh 都得到一致结果：
//   A. 写 FRONTEND_DIST/version.json（commitId / builtAt / indexMd5）
//      → 线上访问 https://你的域名/version.json 一眼核对是否真的更到新版
//   B. cp -rf dist/* → backend/static/（覆盖 index.html 与 assets/，
//      不删除 static 根目录的 logo/dian.jpg 等静态资源）
//   C. 打印 md5 对账，避免一半复制一半没复制
// =======================================================================
function fanshuAlignPlugin() {
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
    name: 'fanshu-align',
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
      console.log('\n📘 [fanshu-align] version.json 写入 dist/version.json:')
      console.log('   commit =', shortCommitId(), '  indexMd5 =', indexMd5)

      // ---------- B. cp -rf dist/* → backend/static/ ----------
      // 关键：使用 recursive cpSync，仅覆盖 dist 里"有的"文件/文件夹，
      // static 根目录下 logo.png / dian.jpg / favicon.svg 等 dist 中没有的资源不会被删
      if (!existsSync(BACKEND_STATIC)) {
        mkdirSync(BACKEND_STATIC, { recursive: true })
      }
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
          `🚨 [fanshu-align] 前后端 index.html 哈希不一致！\n` +
          `   dist:   ${indexMd5}  (${relative(GIT_ROOT, indexHtml)})\n` +
          `   static: ${dstMd5}  (${relative(GIT_ROOT, dstIndex)})\n` +
          `   请手动 rm -rf source/backend/static/assets 后重新 npm run build`
        )
      }
      const dstVersion = join(BACKEND_STATIC, 'version.json')
      writeFileSync(dstVersion, versionPayload, 'utf8')
      console.log('✅ [fanshu-align] dist → backend/static 同步完成，哈希一致:', indexMd5)
      console.log('   → 线上访问 /version.json 可直接核对 commitId 和构建时间\n')
    },
  }
}

export default defineConfig({
  base: './',
  plugins: [react(), fanshuAlignPlugin()],
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
