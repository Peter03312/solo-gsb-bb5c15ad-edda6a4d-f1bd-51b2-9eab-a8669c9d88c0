FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WATERWORKS_DB=/data/waterworks.db \
    SEED_ON_STARTUP=true \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 容器启动即建表并幂等写入演示主数据（应用 lifespan 中完成初始化）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
