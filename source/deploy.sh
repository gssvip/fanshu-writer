#!/bin/bash
set -e

echo "=== 构建前端 ==="
cd /workspace/source/frontend
npm run build

echo "=== 复制产物到 backend/static ==="
cd /workspace/source
rm -rf backend/static/*
cp -r frontend/dist/* backend/static/

echo "=== 更新仓库根目录（供 GitHub Pages main 分支使用）==="
cd /workspace
cp -f source/frontend/dist/index.html .
# 完全清空 assets 目录后再复制，避免旧版本 JS/CSS 残留导致 index.html 引用失配
rm -rf assets
mkdir -p assets
cp -rf source/frontend/dist/assets/* assets/

echo "=== 推送 main 分支（后端 + 静态文件 + 根目录前端）==="
git add .
git commit -m "deploy: $(date +'%Y-%m-%d %H:%M:%S')"
git push origin main

echo "=== 推送 gh-pages 分支（前端静态页面）==="
cd /workspace/source/frontend/dist
git add .
git commit -m "deploy: $(date +'%Y-%m-%d %H:%M:%S')" || true
git push -f origin gh-pages

echo "=== 部署完成 ==="
echo "main: https://fanshu-writer-backend.onrender.com"
echo "gh-pages: https://gssvip.github.io/fanshu-writer"