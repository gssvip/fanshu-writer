---
title: Fanshu Writer Backend
emoji: 🍠
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# 番薯写作后端

Flask 后端服务，提供 AI 写作平台的 API。

## 端点

- `GET /api/templates` - 模板列表（可用于健康检查）
- `POST /api/auth/register` - 注册
- `POST /api/auth/login` - 登录
- `GET /api/auth/me` - 当前用户

## 数据持久化

数据存储在容器内的 `/data` 目录，Hugging Face Spaces 提供持久化存储。

## 部署

此 Space 通过 Dockerfile 自动构建部署。
