#!/bin/bash
set -e

echo "=== 构建前端 ==="
cd /workspace/source/frontend
npm run build

echo "=== 复制产物到 backend/static ==="
cd /workspace/source
rm -rf backend/static/*
cp -r frontend/dist/* backend/static/

echo "=== 推送 main 分支（后端 + 静态文件）==="
git add .
git commit -m "deploy: $(date +'%Y-%m-%d %H:%M:%S')"
git push origin main

echo "=== 推送 gh-pages 分支（前端静态页面）==="
cd frontend/dist
git add .
git commit -m "deploy: $(date +'%Y-%m-%d %H:%M:%S')"
git push -f origin gh-pages

echo "=== 部署完成 ==="
echo "main: https://fanshu-writer-backend.onrender.com"
echo "gh-pages: https://gssvip.github.io/fanshu-writer"