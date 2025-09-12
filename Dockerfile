# Use official Playwright base image (includes Chromium + deps)
FROM mcr.microsoft.com/playwright/python:v1.41.2-jammy

# Fix: install missing fonts (fonts-unifont) to avoid build failure on Railway
RUN apt-get update &&     apt-get install -y --no-install-recommends fonts-unifont &&     rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY . /app

# Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Default envs (can be overridden by Railway Variables)
ENV HEADLESS=1 RUN_ONCE=1

# Run the monitor
CMD ["python", "monitor.py"]
