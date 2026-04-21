FROM python:3.12-slim

WORKDIR /app

# 先装依赖，利用缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor.py .

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

VOLUME ["/data"]

CMD ["python", "monitor.py"]
