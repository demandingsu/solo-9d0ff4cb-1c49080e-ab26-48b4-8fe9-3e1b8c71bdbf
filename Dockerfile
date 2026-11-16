FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY samples ./samples

# SQLite 数据目录（compose 里挂载命名卷持久化）
RUN mkdir -p /data && useradd -r -u 10001 appuser && chown -R appuser:appuser /data
USER appuser

ENV TRANSIT_DB_PATH=/data/transit.db

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
