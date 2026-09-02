#!/usr/bin/env bash
# ======================================================================
# 一键部署脚本（前后端对齐 + 构建门禁）
# 用法：
#   bash deploy.sh                 # 默认：构建+同步+commit+push+部署webhook
#   bash deploy.sh --no-build      # 跳过前端构建
#   bash deploy.sh --no-render     # 不调部署 webhook
#   bash deploy.sh "feat: xxx"     # 自定义 commit message
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

# 部署 webhook：优先读环境变量 DEPLOY_HOOK，避免把钩子 URL 明文写死在脚本仓库里
RENDER_HOOK="${DEPLOY_HOOK:-}"
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
if [ "$NO_RENDER" = "0" ] && [ -z "$RENDER_HOOK" ]; then
  warn "未设置 DEPLOY_HOOK 环境变量，将跳过自动触发部署；设置后可自动调用：export DEPLOY_HOOK=https://api.your-deploy.example.com/xxx"
  NO_RENDER=1
fi

# ---------- 2. 前端构建 + 自动同步 backend/static ----------
# 构建前清旧 hash 资产（多份旧 index-*.js=线上永远看到旧版）
log "2a/6 🔐 build 前清理 backend/static/assets 内所有旧 hash 资产"
OLD_COUNT=0
if [ -d "$STATIC_DIR/assets" ]; then
  while IFS= read -r -d '' f; do
    if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      git rm -f --cached "$f" >/dev/null 2>&1 || true
    fi
    rm -f "$f"
    OLD_COUNT=$((OLD_COUNT+1))
  done < <(find "$STATIC_DIR/assets" -maxdepth 1 -type f \
              \( -name 'index-*.js' -o -name 'index-*.css' \
                 -o -name 'chunk-*.js' -o -name 'chunk-*.css' \
                 -o -name '*.js' -o -name '*.css' \
                 -o -name 'version.json' \) -print0 2>/dev/null)
fi
echo "    ✓ 已删除 $OLD_COUNT 个旧 hash 资产"

if [ "$NO_BUILD" = "1" ]; then
  warn "跳过前端构建（--no-build），直接用现有 dist/"
else
  log "2b/6 前端构建 → tsc + vite build（closeBundle 自动同步 backend/static）"
  (cd "$FRONTEND_DIR" && npm run build) || die "前端构建失败，先修错误再部署"
fi

# 构建后门禁：在后端 static 里 grep 最新打包 JS/HTML，不达标就 die
log "2c/6 🔐 构建门禁：grep backend/static 打包产物是否达标"
# 现在 rollup 配置 chunkFileNames='assets/[hash].js'，所以找最大的 hash js 当主 JS
LATEST_JS=$(find "$STATIC_DIR/assets" -maxdepth 1 -type f \( -name '*.js' \) -printf '%s %p\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}')
[ -f "$LATEST_JS" ] || die "构建后 static/assets 没有任何主 js：$(ls "$STATIC_DIR/assets" 2>/dev/null | tr '\n' ' ')"
echo "    ✓ 最新打包 JS：$LATEST_JS"

HIT_OLD_1=$( (grep -c "命中创作关键词" "$LATEST_JS" 2>/dev/null) || true )
HIT_OLD_2=$( (grep -cE "节点设计师[^\"]*助手可生成情节节点" "$LATEST_JS" 2>/dev/null) || true )
HIT_RANK=$( (grep -oE "榜单分析师" "$LATEST_JS" 2>/dev/null | wc -l) || true )
INDEX_FILES_JS=$(find "$STATIC_DIR/assets" -maxdepth 1 -type f -name '*.js' 2>/dev/null | wc -l)
INDEX_FILES_CSS=$(find "$STATIC_DIR/assets" -maxdepth 1 -type f -name '*.css' 2>/dev/null | wc -l)
HIT_OLD_1=$(echo "$HIT_OLD_1" | tr -d '[:space:]' || echo 0); [ -z "$HIT_OLD_1" ] && HIT_OLD_1=0
HIT_OLD_2=$(echo "$HIT_OLD_2" | tr -d '[:space:]' || echo 0); [ -z "$HIT_OLD_2" ] && HIT_OLD_2=0
HIT_RANK=$(echo  "$HIT_RANK"  | tr -d '[:space:]' || echo 0); [ -z "$HIT_RANK"  ] && HIT_RANK=0
INDEX_FILES_JS=$(echo "$INDEX_FILES_JS" | tr -d '[:space:]' || echo 0); [ -z "$INDEX_FILES_JS" ] && INDEX_FILES_JS=0
INDEX_FILES_CSS=$(echo "$INDEX_FILES_CSS" | tr -d '[:space:]' || echo 0); [ -z "$INDEX_FILES_CSS" ] && INDEX_FILES_CSS=0
# 敏感串门禁：源码绝不能含 fanshu-writer / gssvip / onrender.com 硬编码
HIT_DOMAIN=$( (grep -oE "onrender\.com|fanshu-writer|gssvip|github\.pages" "$STATIC_DIR/index.html" 2>/dev/null | sort -u | tr '\n' ',') || echo "" )
echo "    ✓ static/assets/*.js 数量：$INDEX_FILES_JS；  *.css 数量：$INDEX_FILES_CSS"
echo "    ✓ 命中创作关键词：$HIT_OLD_1 次（期望=0）"
echo "    ✓ 节点设计师提示：   $HIT_OLD_2 次（期望=0）"
echo "    ✓ 榜单分析师：       $HIT_RANK 次（期望≥2）"
[ -n "$HIT_DOMAIN" ] && echo "    🔴 源码敏感串命中: $HIT_DOMAIN"

[ "$INDEX_FILES_JS" -ge 1 ] || die "门禁失败：static/assets 没有 js"
[ "$INDEX_FILES_CSS" -ge 1 ] || die "门禁失败：static/assets 没有 css"
[ "$HIT_OLD_1" = "0" ] || die "门禁失败：最新 JS 里仍含「命中创作关键词」$HIT_OLD_1 次"
[ "$HIT_OLD_2" = "0" ] || die "门禁失败：最新 JS 里仍含「节点设计师.*助手可生成情节节点」$HIT_OLD_2 次"
[ "$HIT_RANK" -ge 2 ] || die "门禁失败：最新 JS 里「榜单分析师」只有 $HIT_RANK 次，期望≥2"
[ -z "$HIT_DOMAIN" ] || die "门禁失败：打包后 HTML 源码仍含敏感标识: $HIT_DOMAIN，先修完再部署"

# 后端 Python 语法门禁
log "2d/6 🔐 构建门禁：后端 Python 语法校验"
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
      die "后端语法错误！文件：$pyf 。本地执行 python3 -m py_compile $pyf 查看具体行号"
    fi
  fi
done
echo "    ✓ 全部后端 py 语法 OK"

# ---------- 3. 二次对账 ----------
log "3/6 哈希二次对账：dist/index.html == backend/static/index.html"
[ -f "$DIST_DIR/index.html" ]            || die "DIST_DIR/index.html 不存在：$DIST_DIR/index.html"
[ -f "$STATIC_DIR/index.html" ]          || die "STATIC_DIR/index.html 不存在：$STATIC_DIR/index.html"
[ -f "$STATIC_DIR/version.json" ]        || die "version.json 没写入 backend/static/"

MD5_DIST=$(md5sum   "$DIST_DIR/index.html"   | awk '{print $1}')
MD5_STATIC=$(md5sum "$STATIC_DIR/index.html" | awk '{print $1}')
if [ "$MD5_DIST" != "$MD5_STATIC" ]; then
  die "哈希不一致！dist=$MD5_DIST  static=$MD5_STATIC → 重新 bash deploy.sh"
fi
VER_COMMIT=$(node -e "console.log(JSON.parse(require('fs').readFileSync('$STATIC_DIR/version.json','utf8')).commit)")
VER_BUILT=$(node -e  "console.log(JSON.parse(require('fs').readFileSync('$STATIC_DIR/version.json','utf8')).builtAt.slice(0,19))")
echo "    ✓ indexMd5 = $MD5_DIST"
echo "    ✓ commit   = $VER_COMMIT"
echo "    ✓ builtAt  = $VER_BUILT (UTC)"

# ---------- 4. git add → commit → push origin main ----------
log "4/6 Git：add + commit + push origin main"
git add -f "$DIST_DIR/index.html" "$DIST_DIR"/assets/*.js "$DIST_DIR"/assets/*.css "$DIST_DIR/version.json" 2>/dev/null || true
git add -u "$STATIC_DIR" 2>/dev/null || true
if [ -d "$STATIC_DIR/assets" ]; then
  git add -f "$STATIC_DIR/index.html" \
            $(find "$STATIC_DIR/assets" -maxdepth 1 -type f \( -name '*.js' -o -name '*.css' \) 2>/dev/null) \
            "$STATIC_DIR/version.json" 2>/dev/null || true
fi
git add -A
if git diff --cached --quiet; then
  warn "没有可提交的改动；继续尝试 push"
else
  if [ -n "$CUSTOM_MSG" ]; then
    MSG="$CUSTOM_MSG"
  else
    CHANGES=""
    git diff --cached --name-only | grep -q "src/"           && CHANGES="${CHANGES}前端 "
    git diff --cached --name-only | grep -q "backend/static" && CHANGES="${CHANGES}静态产物 "
    git diff --cached --name-only | grep -q "blueprints/"    && CHANGES="${CHANGES}后端API "
    git diff --cached --name-only | grep -qE "vite\.config|deploy\.sh|package\.json|\.gitignore|render\.yaml" && CHANGES="${CHANGES}工程配置 "
    [ -z "$CHANGES" ] && CHANGES="杂项"
    MSG="deploy: ${CHANGES}（${VER_COMMIT} · ${VER_BUILT}Z · md5=${MD5_DIST:0:8}）"
  fi
  git -c user.name="$GIT_USER" -c user.email="$GIT_EMAIL" commit -m "$MSG" || die "git commit 失败"
fi

if git remote get-url origin >/dev/null 2>&1; then
  git push origin main || die "git push 失败。检查 GitHub PAT 权限/网络"
else
  die "未配置 git remote origin，请先 git remote add origin <你的仓库>"
fi

NEW_HEAD=$(git rev-parse --short HEAD)
echo "    ✓ push 完成，HEAD=$NEW_HEAD  (main)"

# ---------- 5. 触发部署 webhook ----------
if [ "$NO_RENDER" = "1" ]; then
  warn "跳过自动部署触发（--no-render 或未配置 DEPLOY_HOOK）"
else
  log "5/6 触发部署 webhook"
  HTTP_CODE=$(curl -sS -X POST "$RENDER_HOOK" -o /tmp/deploy_resp.json -w "%{http_code}" || echo "000")
  if [ "$HTTP_CODE" = "202" ] || [ "$HTTP_CODE" = "200" ]; then
    echo "    ✓ 部署钩子已接受（HTTP $HTTP_CODE），响应=$(cat /tmp/deploy_resp.json 2>/dev/null || echo N/A)"
  else
    die "部署钩子返回非 202：HTTP=$HTTP_CODE 响应=$(cat /tmp/deploy_resp.json 2>/dev/null || echo N/A)"
  fi
fi

# ---------- 6. 门禁汇总 ----------
log "6/6 门禁汇总"
cat <<EOF
    最新主JS: $LATEST_JS
    红圈文案残留：命中创作关键词=$HIT_OLD_1  ·  节点设计师提示=$HIT_OLD_2
    榜单分析师出现次数: $HIT_RANK
    static/assets: js=$INDEX_FILES_JS 份  css=$INDEX_FILES_CSS 份
EOF
log "部署已启动（2~5 分钟后生效）"
cat <<EOF

📋 上线核对：curl -s https://<你的线上域名>/version.json
    期望 commit = "$NEW_HEAD"，indexMd5 前 8 = "${MD5_DIST:0:8}"，builtAt = "$VER_BUILT"
EOF
