# 直接用 Playwright 官方基底，已內含 Chromium + 依賴
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

# 補中文字型，避免字元亂碼（很輕量）
RUN apt-get update &&     apt-get install -y --no-install-recommends fonts-unifont &&     rm -rf /var/lib/apt/lists/*

# 讓 Python 輸出即時、且少產生快取
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright  # 使用鏡像內建的瀏覽器，**不要**再安裝

WORKDIR /app
COPY . /app

# 只裝需要的第三方套件（Playwright已在基底鏡像，不必再裝）
RUN pip install --no-cache-dir -r requirements.txt

# 預設環境（Railway Variables 會覆蓋）
ENV HEADLESS=1 RUN_ONCE=1

CMD ["python", "monitor.py"]
