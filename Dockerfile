# FROM python:3.11-slim

# WORKDIR /app

# # 安装 Python 依赖
# COPY requirements.txt .
# RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# COPY . .

# EXPOSE 8000

# CMD ["python", "main.py"]

FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render provides PORT
CMD ["sh", "-c", "uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
