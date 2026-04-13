# KMGroup 生产管理系统
FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖（cascadio 需要编译环境）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 环境变量（启动时可通过 docker-compose 或 -e 覆盖）
ENV APP_HOST=0.0.0.0
ENV APP_PORT=2006
ENV APP_DEBUG=false
ENV ENABLE_SCHEDULER=true

EXPOSE 2006

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "2006"]
