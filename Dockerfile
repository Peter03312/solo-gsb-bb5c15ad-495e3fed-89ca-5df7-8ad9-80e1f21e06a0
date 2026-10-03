# 事故复盘时间线 —— 仅使用 Python 3.11 标准库，无需安装任何第三方包
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8800 \
    HOST=0.0.0.0 \
    DB_PATH=/data/timeline.db \
    SEED_DEMO=1

WORKDIR /app

# 无第三方依赖：requirements.txt 保留为空占位（如后续需要可直接添加并恢复 pip 步骤）
COPY requirements.txt ./
COPY app ./app

# 数据持久化卷（SQLite 数据库）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8800

# 启动时自动建表初始化数据库（已存在则不动），随后启动 HTTP 服务
CMD ["python", "-m", "app.server"]
