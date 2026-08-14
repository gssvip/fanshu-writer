#!/bin/bash
set -e

echo "=== 构建前端 ==="
cd /workspace/source/frontend
npm run build

echo "=== 生成版本指纹 version.json（供前端自检刷新）==="
# 从 dist/index.html 提取 JS 文件名作为版本指纹，加时间戳保证每次部署唯一
JS_FILE=$(grep -oE 'assets/index-[^"]+\.js' dist/index.html | head -1)
DEPLOY_TS=$(date +%s)
cat > dist/version.json <<EOF
{"v": "${DEPLOY_TS}", "js": "${JS_FILE}", "time": "$(date +'%Y-%m-%d %H:%M:%S')"}
EOF
echo "version.json 内容: $(cat dist/version.json)"

# 给 JS/CSS 引用加版本戳，强制浏览器重新下载（避免 Vite hash 未变时中间缓存不刷新）
sed -i "s|src=\"\\(./assets/index-[^\"]*\\.js\\)\"|src=\"\\1?v=${DEPLOY_TS}\"|g" dist/index.html
sed -i "s|href=\"\\(./assets/index-[^\"]*\\.css\\)\"|href=\"\\1?v=${DEPLOY_TS}\"|g" dist/index.html
echo "index.html 更新后引用:"
grep -oE 'assets/index-[^" ]+\?v=[0-9]+' dist/index.html || true

echo "=== 复制产物到 backend/static ==="
cd /workspace/source
rm -rf backend/static/*
cp -r frontend/dist/* backend/static/

echo "=== 更新仓库根目录（供 GitHub Pages main 分支使用）==="
cd /workspace
cp -f source/frontend/dist/index.html .
cp -f source/frontend/dist/version.json .
# 完全清空 assets 目录后再复制，避免旧版本 JS/CSS 残留导致 index.html 引用失配
rm -rf assets
mkdir -p assets
cp -rf source/frontend/dist/assets/* assets/

echo "=== 推送 main 分支（后端 + 静态文件 + 根目录前端）==="
git add .
git commit -m "deploy: $(date +'%Y-%m-%d %H:%M:%S')" || true
git push origin main

echo "=== 推送 gh-pages 分支（前端静态页面）==="
# dist 被 .gitignore 忽略，需 init 独立临时仓库专推 gh-pages
# 全局已配置 credential.helper=store，子仓库继承，无需再输 token
cd /workspace/source/frontend/dist
rm -rf .git
git init -q
git config user.email "deploy@fanshu.dev"
git config user.name "fanshu-deploy"
git config credential.helper store
git remote add origin https://github.com/gssvip/fanshu-writer.git
git add -A
git commit -m "deploy: $(date +'%Y-%m-%d %H:%M:%S')" -q || true
# 本地分支为 master/main，推到远程 gh-pages 分支
git push -f origin HEAD:gh-pages

echo "=== 触发 Render 自动部署 ==="
# Render Deploy Hook：push 后自动触发后端部署，无需手动刷新 Render Dashboard
# Hook URL 从环境变量读取（避免密钥泄露到 git 历史）
# 本机配置：echo 'export RENDER_DEPLOY_HOOK_URL="https://api.render.com/deploy/srv-xxx?key=yyy"' >> ~/.bashrc
RENDER_DEPLOY_HOOK_URL="${RENDER_DEPLOY_HOOK_URL:-}"
if [ -z "$RENDER_DEPLOY_HOOK_URL" ]; then
    echo "⚠️  未配置 RENDER_DEPLOY_HOOK_URL 环境变量，跳过自动触发 Render"
    echo "配置方法：echo 'export RENDER_DEPLOY_HOOK_URL=\"https://api.render.com/deploy/srv-xxx?key=yyy\"' >> ~/.bashrc && source ~/.bashrc"
else
    RENDER_STATUS=$(curl -s -o /tmp/render_deploy_resp.json -w "%{http_code}" -X POST "$RENDER_DEPLOY_HOOK_URL")
    echo "Render 响应: HTTP $RENDER_STATUS"
    cat /tmp/render_deploy_resp.json 2>/dev/null
    if [ "$RENDER_STATUS" = "200" ] || [ "$RENDER_STATUS" = "202" ]; then
        echo "✅ Render 部署已触发"
    else
        echo "⚠️  Render 部署触发失败（HTTP $RENDER_STATUS），可手动到 Render Dashboard 触发"
    fi
fi

echo "=== 部署完成 ==="
echo "main: https://fanshu-writer-backend.onrender.com"
echo "gh-pages: https://gssvip.github.io/fanshu-writer"