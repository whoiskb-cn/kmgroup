# KMGroup 生产管理系统
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

# 使用阿里云 Debian 镜像（解决国内 502 问题）
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian*.list 2>/dev/null || true \
    && echo 'deb https://mirrors.aliyun.com/debian/ bookworm main' > /etc/apt/sources.list \
    && echo 'deb https://mirrors.aliyun.com/debian-security/ bookworm-security main' >> /etc/apt/sources.list \
    && apt-get update --fix-missing && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# 阶段二：最终镜像
FROM python:3.12-slim-bookworm

WORKDIR /app

# 复制编译好的 site-packages 和应用代码
COPY --from=builder /root/.local /root/.local
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages

# 只复制运行时必需的文件
COPY main.py .
COPY models.py .
COPY database.py .
COPY security.py .
COPY auth_session.py .
COPY product_service.py .
COPY seq_utils.py .
COPY import_utils.py .
COPY wechat_runtime.py .
COPY requirements.txt .
COPY routers/ ./routers/
COPY static/ ./static/
COPY config/  ./config/

# 运行时依赖
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian*.list 2>/dev/null || true \
    && echo 'deb https://mirrors.aliyun.com/debian/ bookworm main' > /etc/apt/sources.list \
    && echo 'deb https://mirrors.aliyun.com/debian-security/ bookworm-security main' >> /etc/apt/sources.list \
    && apt-get update --fix-missing && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /root/.cache

ENV PATH=/root/.local/bin:$PATH
ENV APP_HOST=0.0.0.0
ENV APP_PORT=2006
ENV APP_DEBUG=false
ENV ENABLE_SCHEDULER=true

EXPOSE 2006

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "2006"]
