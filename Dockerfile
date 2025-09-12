# 直接用 Playwright 官方基底（含 Chromium 與相依）
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 補中文字型，避免亂碼
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-unifont && \
    rm -rf /var/lib/apt/lists/*

# 乾淨的環境變數設定
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY . /app

# 只安裝需要的套件（Playwright 基底鏡像已自帶瀏覽器）
RUN pip install --no-cache-dir -r requirements.txt

# 預設環境（Railway Variables 會覆蓋）
ENV HEADLESS=1 RUN_ONCE=1

CMD ["python", "monitor.py"]
