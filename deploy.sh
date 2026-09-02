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
# =============【★ 永久防呆·第一层·build 前先从 git 彻底移除旧 static 资产 ★】=============
# 哪怕 vite fanshuAlignPlugin 未来因为配置变更不触发、或者 closeBundle 钩子没跑，
# 这一步也能保证 GitHub 仓库里 backend/static/assets 不会同时存在多份旧 JS（index-*.js/css 带 hash 的文件）
# 及 KaTeX 字体——它们是导致"用户刷新永远看到旧版"的根因（Service Worker/缓存优先命中仓库里的旧hash文件）。
# static 根目录的 logo.png / dian.jpg / favicon.svg / icons.svg / manifest.json / icon-192/512 等不动。
log "2a/6 🔐 build 前强制清理 backend/static/assets 内所有旧 hash 资产（index-*/chunk-*/KaTeX-*）"
OLD_COUNT=0
if [ -d "$STATIC_DIR/assets" ]; then
  while IFS= read -r -d '' f; do
    # 只处理 git 已跟踪的或已存在的本地文件，确保 commit 里真的没旧 hash
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      git rm -f --cached "$f" >/dev/null 2>&1 || true
    fi
    rm -f "$f"
    OLD_COUNT=$((OLD_COUNT+1))
  done < <(find "$STATIC_DIR/assets" -maxdepth 1 -type f \
              \( -name 'index-*.js' -o -name 'index-*.css' \
                 -o -name 'chunk-*.js' -o -name 'chunk-*.css' \
                 -o -name 'KaTeX_*' \
                 -o -name 'version.json' \) -print0 2>/dev/null)
fi
echo "    ✓ 已删除 $OLD_COUNT 个旧 hash 资产"

if [ "$NO_BUILD" = "1" ]; then
  warn "跳过前端构建（--no-build），直接用现有 dist/"
else
  log "2b/6 前端构建 → tsc + vite build（build 结束钩子自动同步到 backend/static 并校验哈希）"
  (cd "$FRONTEND_DIR" && npm run build) || die "前端构建失败，先修错误再部署"
fi

# =============【★ 永久防呆·第二层·构建后 Grep 内容门禁（不达标就 die）★】=============
# 直接在"用户线上实际加载的打包 JS 里"grep，不用源码。避免"源码改了但构建打进去的还是旧版"
log "2c/6 🔐 构建门禁：grep backend/static 最新打包 JS，不达标就拒绝 push"
LATEST_JS=$(find "$STATIC_DIR/assets" -maxdepth 1 -type f -name 'index-*.js' | head -1)
[ -f "$LATEST_JS" ] || die "构建后 static/assets 没有任何 index-*.js：static/assets 下文件：$(ls "$STATIC_DIR/assets" 2>/dev/null | tr '\n' ' ')"
echo "    ✓ 最新打包 JS：$LATEST_JS（只有 1 份是正确状态）"

HIT_OLD_1=$( (grep -c "命中创作关键词" "$LATEST_JS" 2>/dev/null) || true )
HIT_OLD_2=$( (grep -cE "节点设计师[^\"]*助手可生成情节节点" "$LATEST_JS" 2>/dev/null) || true )
HIT_RANK=$( (grep -oE "榜单分析师" "$LATEST_JS" 2>/dev/null | wc -l) || true )
INDEX_FILES_JS=$(find "$STATIC_DIR/assets" -maxdepth 1 -type f -name 'index-*.js' 2>/dev/null | wc -l)
INDEX_FILES_CSS=$(find "$STATIC_DIR/assets" -maxdepth 1 -type f -name 'index-*.css' 2>/dev/null | wc -l)
# 强制转纯数字（strip whitespace/newlines，防止 bash 把 0\n 当字符串判不等于 0）
HIT_OLD_1=$(echo "$HIT_OLD_1" | tr -d '[:space:]' || echo 0); [ -z "$HIT_OLD_1" ] && HIT_OLD_1=0
HIT_OLD_2=$(echo "$HIT_OLD_2" | tr -d '[:space:]' || echo 0); [ -z "$HIT_OLD_2" ] && HIT_OLD_2=0
HIT_RANK=$(echo  "$HIT_RANK"  | tr -d '[:space:]' || echo 0); [ -z "$HIT_RANK"  ] && HIT_RANK=0
INDEX_FILES_JS=$(echo "$INDEX_FILES_JS" | tr -d '[:space:]' || echo 0); [ -z "$INDEX_FILES_JS" ] && INDEX_FILES_JS=0
INDEX_FILES_CSS=$(echo "$INDEX_FILES_CSS" | tr -d '[:space:]' || echo 0); [ -z "$INDEX_FILES_CSS" ] && INDEX_FILES_CSS=0
echo "    ✓ static/assets/index-*.js 数量：$INDEX_FILES_JS（期望=1）"
echo "    ✓ static/assets/index-*.css 数量：$INDEX_FILES_CSS（期望=1）"
echo "    ✓ 命中创作关键词：$HIT_OLD_1 次（期望=0）"
echo "    ✓ 节点设计师提示：   $HIT_OLD_2 次（期望=0）"
echo "    ✓ 榜单分析师：       $HIT_RANK 次（期望≥2）"

[ "$INDEX_FILES_JS" = "1" ] || die "门禁失败：static/assets 有 $INDEX_FILES_JS 份 index-*.js，必须=1（旧 hash 没删干净？）"
[ "$INDEX_FILES_CSS" = "1" ] || die "门禁失败：static/assets 有 $INDEX_FILES_CSS 份 index-*.css，必须=1"
[ "$HIT_OLD_1" = "0" ] || die "门禁失败：最新 JS 里仍含「命中创作关键词」$HIT_OLD_1 次（红圈文字没真正打包进去删除）"
[ "$HIT_OLD_2" = "0" ] || die "门禁失败：最新 JS 里仍含「节点设计师.*助手可生成情节节点」$HIT_OLD_2 次（红圈第二行没真正删除）"
[ "$HIT_RANK" -ge 2 ] || die "门禁失败：最新 JS 里「榜单分析师」只有 $HIT_RANK 次，期望≥2（说明 BUILTIN_ROLES 没真正打进去）"

# =============【★ 永久防呆·第二层-B · 后端 Python .py 语法门禁（不达标 die）★】=============
# Render 已经三次因为后端 SyntaxError 启动失败 -> 持续 fallback 老版本 -> 用户"刷新还是旧界面"
# 直接 py_compile 所有核心后端 py；有语法错误绝不允许 push。任何新增 blueprint 请加进这个数组。
log "2d/6 🔐 构建门禁：后端 Python 语法校验（py_compile 不通过就拒绝 push）"
BACKEND_PYS=(
  "source/backend/app.py"
  "source/backend/blueprints/chat_collab_bp.py"
  "source/backend/blueprints/novel_rank_bp.py"
  "source/backend/blueprints/ai_config_bp.py"
  "source/backend/blueprints/health_bp.py"
  "source/backend/blueprints/context_ranker.py"
  "source/backend/blueprints/post_gen_validator.py"
)
for pyf in "${BACKEND_PYS[@]}"; do
  if [ -f "$pyf" ]; then
    if python3 -m py_compile "$pyf" 2>/dev/null; then
      echo "    ✓ OK  $pyf"
    else
      die "后端语法错误！文件：$pyf 。本地执行 python3 -m py_compile $pyf 查看具体行号，修完再 bash deploy.sh"
    fi
  fi
done
echo "    ✓ 全部后端 py 语法 OK"

# ---------- 3. 二次对账（防 vite 插件没跑/老版本 vite 没触发 closeBundle） ----------
log "3/6 哈希二次对账：dist/index.html == backend/static/index.html"
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
log "4/6 Git：add + commit + push origin main"
# 4a. 强制把「前端构建产物 & static 目录」纳入版本控制（即使 .gitignore 错改也能兜底）
git add -f "$DIST_DIR/index.html" "$DIST_DIR"/assets/*.js "$DIST_DIR"/assets/*.css "$DIST_DIR/version.json" 2>/dev/null || true
# 4a+【★ 第三层防呆 ★】：先 git add -u（把刚才 2a 步骤里 git rm --cached 的旧 hash 资产真正标记为 delete），
# 再 git add 新文件，保证 commit 里同时包含"删除旧文件"和"添加新文件"（否则可能只加新文件，旧文件还留在仓库里）
git add -u "$STATIC_DIR" 2>/dev/null || true
git add -f "$STATIC_DIR/index.html" "$STATIC_DIR"/assets/*.js "$STATIC_DIR"/assets/*.css "$STATIC_DIR/version.json" 2>/dev/null || true
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
  log "5/6 触发 Render 部署 webhook"
  HTTP_CODE=$(curl -sS -X POST "$RENDER_HOOK" -o /tmp/fanshu_render.json -w "%{http_code}" || echo "000")
  if [ "$HTTP_CODE" = "202" ] || [ "$HTTP_CODE" = "200" ]; then
    echo "    ✓ Render 已接受部署（HTTP $HTTP_CODE），body=$(cat /tmp/fanshu_render.json 2>/dev/null || echo N/A)"
  else
    die "Render 部署钩子返回非 202：HTTP=$HTTP_CODE body=$(cat /tmp/fanshu_render.json 2>/dev/null || echo N/A)"
  fi
fi

# ---------- 6. 打印门禁汇总（部署完了也留个底） ----------
log "6/6 门禁汇总"
cat <<EOF
    ┌─────────────────────────────────────────────────────────────────┐
    │  🔐 deploy.sh 三层防呆（以后不管我记不记得，自动清旧 JS）        │
    │  1st: build 前  git rm --cached + rm 所有旧 index/chunk/KaTeX   │
    │  2nd: build 后  grep 打包 JS 门禁（红圈=0/榜单≥2/文件数=1）     │
    │  3rd: commit 前 git add -u static/（真正把旧 hash delete 进仓库）│
    └─────────────────────────────────────────────────────────────────┘
    最新 JS 文件：$LATEST_JS
    红圈文案残留：命中创作关键词=$HIT_OLD_1  ·  节点设计师提示=$HIT_OLD_2
    榜单分析师  ：$HIT_RANK 次
    static/assets index-*.js = $INDEX_FILES_JS 份（1=正确）
EOF

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
