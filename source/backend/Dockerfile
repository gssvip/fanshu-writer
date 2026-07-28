# 蚂蚁写作后端 - Hugging Face Spaces Docker 镜像
# 使用 Python 3.11 slim 镜像减小体积
FROM python:3.11-slim

# 设置工作目录（Hugging Face Spaces 强制要求 /app）
WORKDIR /app

# 系统依赖：libxml2/libxslt 用于 ebooklib, libjpeg 用于 Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# Hugging Face Spaces 强制使用 7860 端口
ENV PORT=7860
ENV PYTHONUNBUFFERED=1
# 持久化数据目录（Hugging Face Spaces 会保留 /data 内容）
ENV FANSHU_DATA_DIR=/data

# 创建持久化目录
RUN mkdir -p /data

# 暴露端口
EXPOSE 7860

# 启动命令
CMD ["python", "app.py"]
