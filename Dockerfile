# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY manage.py ./
COPY core ./core
COPY app ./app
COPY scripts ./scripts
COPY README.md ./
COPY .env.example ./

EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
