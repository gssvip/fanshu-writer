#!/usr/bin/env bash
# ======================================================================
# 粉丝写作 / 蚂蚁写作 —— 一键部署脚本（根治"前后端不一"）
# 用法：
#   bash deploy.sh                 # 默认：构建+同步+commit+push+Render
#   bash deploy.sh --no-build      # 跳过前端构建（已经 build 过，只想提交推送）
#   bash deploy.sh --no-render     # 不调 Render webhook
#   bash deploy.sh "feat: xxx"     # 自定义 commit message
#
# 产出核对（部署完成后用户直接访问）：
#   https://<你的 render 域名>/version.json   ← 显示 commitId / builtAt / md5
# ======================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

NO_BUILD=0
NO_RENDER=0
CUSTOM_MSG=""
for arg in "$@"; do
  case "$arg" in
    --no-build)  NO_BUILD=1 ;;
    --no-render) NO_RENDER=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *)
      if [ -n "$CUSTOM_MSG" ]; then CUSTOM_MSG="$CUSTOM_MSG $arg"; else CUSTOM_MSG="$arg"; fi ;;
  esac
done

RENDER_HOOK="https://api.render.com/deploy/srv-d9in6n741pts73bgfn0g?key=iq7a9DTpjHY"
FRONTEND_DIR="$SCRIPT_DIR/source/frontend"
STATIC_DIR="$SCRIPT_DIR/source/backend/static"
DIST_DIR="$FRONTEND_DIR/dist"
GIT_USER="${GIT_USER_NAME:-Trae}"
GIT_EMAIL="${GIT_USER_EMAIL:-trae@local}"

log()   { printf "\n\033[1;36m==> %s\033[0m\n" "$*"; }
warn()  { printf "\n\033[1;33m⚠️  %s\033[0m\n" "$*" >&2; }
die()   { printf "\n\033[1;31m🚨 %s\033[0m\n" "$*" >&2; exit 1; }

# ---------- 1. 环境检查 ----------
log "1/5 环境检查"
command -v node >/dev/null 2>&1 || die "需要 node.js（>=18）才能构建前端"
command -v npm  >/dev/null 2>&1 || die "需要 npm"
command -v git  >/dev/null 2>&1 || die "需要 git"
[ -f "$FRONTEND_DIR/package.json" ] || die "找不到 $FRONTEND_DIR/package.json"
[ -f "$SCRIPT_DIR/render.yaml" ]   || warn "render.yaml 不在项目根，你可能在错误目录运行本脚本"

# ---------- 2. 前端构建 + 自动同步 backend/static（由 vite fanshuAlignPlugin 完成） ----------
if [ "$NO_BUILD" = "1" ]; then
  warn "跳过前端构建（--no-build），直接用现有 dist/"
else
  log "2/5 前端构建 → tsc + vite build（build 结束钩子自动同步到 backend/static 并校验哈希）"
  (cd "$FRONTEND_DIR" && npm run build) || die "前端构建失败，先修错误再部署"
fi

# ---------- 3. 二次对账（防 vite 插件没跑/老版本 vite 没触发 closeBundle） ----------
log "3/5 哈希二次对账：dist/index.html == backend/static/index.html"
[ -f "$DIST_DIR/index.html" ]            || die "DIST_DIR/index.html 不存在：$DIST_DIR/index.html"
[ -f "$STATIC_DIR/index.html" ]          || die "STATIC_DIR/index.html 不存在：$STATIC_DIR/index.html"
[ -f "$STATIC_DIR/version.json" ]        || die "version.json 没写入 backend/static/（vite 插件没触发？）"

MD5_DIST=$(md5sum   "$DIST_DIR/index.html"   | awk '{print $1}')
MD5_STATIC=$(md5sum "$STATIC_DIR/index.html" | awk '{print $1}')
if [ "$MD5_DIST" != "$MD5_STATIC" ]; then
  die "哈希不一致！dist=$MD5_DIST  static=$MD5_STATIC  → 重新 bash deploy.sh"
fi
VER_COMMIT=$(node -e "console.log(JSON.parse(require('fs').readFileSync('$STATIC_DIR/version.json','utf8')).commit)")
VER_BUILT=$(node -e  "console.log(JSON.parse(require('fs').readFileSync('$STATIC_DIR/version.json','utf8')).builtAt.slice(0,19))")
echo "    ✓ indexMd5   = $MD5_DIST"
echo "    ✓ commit     = $VER_COMMIT"
echo "    ✓ builtAt    = $VER_BUILT (UTC ISO)"

# ---------- 4. git add → commit → push origin main ----------
log "4/5 Git：add + commit + push origin main"
# 4a. 强制把「前端构建产物 & static 目录」纳入版本控制（即使 .gitignore 错改也能兜底）
git add -f "$DIST_DIR/index.html" "$DIST_DIR"/assets/*.js "$DIST_DIR"/assets/*.css "$DIST_DIR/version.json" 2>/dev/null || true
git add -f "$STATIC_DIR/index.html" "$STATIC_DIR"/assets/*.js "$STATIC_DIR"/assets/*.css "$STATIC_DIR/version.json" || true
# 4b. 把一切改动加进去
git add -A
# 4c. 若无改动，跳过 commit
if git diff --cached --quiet; then
  warn "没有可提交的改动；继续尝试 push"
else
  if [ -n "$CUSTOM_MSG" ]; then
    MSG="$CUSTOM_MSG"
  else
    # 自动根据变更写 commit message，避免每次都是相同标题
    CHANGES=""
    git diff --cached --name-only | grep -q "src/"           && CHANGES="${CHANGES}前端 "
    git diff --cached --name-only | grep -q "backend/static" && CHANGES="${CHANGES}静态产物 "
    git diff --cached --name-only | grep -q "blueprints/"    && CHANGES="${CHANGES}后端API "
    git diff --cached --name-only | grep -q "vite.config\|deploy.sh\|package.json\|.gitignore\|render.yaml" && CHANGES="${CHANGES}工程配置 "
    [ -z "$CHANGES" ] && CHANGES="杂项"
    MSG="deploy: ${CHANGES}（${VER_COMMIT} · ${VER_BUILT}Z · md5=${MD5_DIST:0:8}）"
  fi
  git -c user.name="$GIT_USER" -c user.email="$GIT_EMAIL" commit -m "$MSG" || die "git commit 失败"
fi

# 4d. push（remote 需要提前配好；之前项目已配好 origin）
if git remote get-url origin >/dev/null 2>&1; then
  git push origin main || die "git push 失败。检查 GitHub PAT 权限/网络"
else
  die "未配置 git remote origin，请先 git remote add origin <你的仓库>"
fi

NEW_HEAD=$(git rev-parse --short HEAD)
echo "    ✓ push 完成，HEAD=$NEW_HEAD  (main)"

# ---------- 5. 触发 Render 部署 ----------
if [ "$NO_RENDER" = "1" ]; then
  warn "跳过 Render deploy（--no-render）；手动到 Render 控制台触发部署"
else
  log "5/5 触发 Render 部署 webhook"
  HTTP_CODE=$(curl -sS -X POST "$RENDER_HOOK" -o /tmp/fanshu_render.json -w "%{http_code}" || echo "000")
  if [ "$HTTP_CODE" = "202" ] || [ "$HTTP_CODE" = "200" ]; then
    echo "    ✓ Render 已接受部署（HTTP $HTTP_CODE），body=$(cat /tmp/fanshu_render.json 2>/dev/null || echo N/A)"
  else
    die "Render 部署钩子返回非 202：HTTP=$HTTP_CODE body=$(cat /tmp/fanshu_render.json 2>/dev/null || echo N/A)"
  fi
fi

# ---------- 收尾：给用户一条核对命令 ----------
log "部署已启动（Render 免费版 2~5 分钟后生效）"
cat <<EOF

📋 上线核对命令（5 分钟后执行，或直接用浏览器打开）：
    curl -s https://<你的 render 域名>/version.json

期望看到：
    commit = "$NEW_HEAD"
    indexMd5 的前 8 位 = "${MD5_DIST:0:8}"
    builtAt = "$VER_BUILT"

如果对不上 → 再跑一次 bash deploy.sh 或者到 Render 控制台看 deploy 日志。
EOF
