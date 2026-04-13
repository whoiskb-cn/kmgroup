# KMGroup 生产管理系统
# 阶段一：编译安装依赖
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update --fix-missing && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# 阶段二：最终镜像（不含编译工具）
FROM python:3.12-slim-bookworm

WORKDIR /app

# 复制编译好的 site-packages
COPY --from=builder /root/.local /root/.local
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# 安装运行时依赖（不含 build-essential）
RUN apt-get update --fix-missing && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 复制应用代码
COPY . .

ENV PATH=/root/.local/bin:$PATH
ENV APP_HOST=0.0.0.0
ENV APP_PORT=2006
ENV APP_DEBUG=false
ENV ENABLE_SCHEDULER=true

EXPOSE 2006

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "2006"]
