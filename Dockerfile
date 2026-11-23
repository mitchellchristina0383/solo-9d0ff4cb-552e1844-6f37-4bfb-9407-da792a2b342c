FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/inspector.db \
    VOLUME_PATH=/volume \
    POLL_INTERVAL_SECONDS=1

WORKDIR /app

# 系统 tzdata 与依赖
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY inspector ./inspector
COPY wsgi.py ./

RUN mkdir -p /data /volume
VOLUME ["/data", "/volume"]

EXPOSE 8000

# 单进程多线程：后台调度线程与 Flask 请求线程共享同一内存状态与 SQLite
CMD ["python", "wsgi.py"]
