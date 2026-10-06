FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据文件位置, 可用 -e DB_PATH=... 覆盖
ENV DB_PATH=/data/water.db
VOLUME /data

EXPOSE 8000

# 启动即自动建库建表(init_db), 无需额外初始化步骤
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
