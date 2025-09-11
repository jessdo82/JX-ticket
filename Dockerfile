# syntax=docker/dockerfile:1
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates fonts-liberation libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxrandr2 libxdamage1 libpango-1.0-0 \
    libasound2 libatspi2.0-0 libx11-6 libx11-xcb1 libxext6 libxfixes3 libglib2.0-0 \
    libgbm1 xvfb curl unzip && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY monitor.py .

ENV HEADLESS=1 RUN_ONCE=1
CMD ["python", "monitor.py"]
