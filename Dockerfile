# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Excel / HTML 转 PDF 服务 —— 单镜像全量运行环境
#
# 构建（在 pdf-converter 目录执行，构建上下文为当前目录）：
#   docker build -t pdf-converter:latest .
#
# 运行：
#   docker run -d --name converter \
#     -p 8000:8000 \
#     -e API_KEYS=your-secret-key \
#     -v converter-data:/data \
#     --memory=4g --cpus=2 \
#     pdf-converter:latest
#
# 验证：
#   curl http://localhost:8000/healthz
# ---------------------------------------------------------------------------

FROM python:3.11-slim-bookworm

# ---------------------------------------------------------------------------
# 镜像加速（国内网络）：构建时可用 --build-arg 覆盖为官方源
#   docker build --build-arg APT_MIRROR=deb.debian.org \
#                --build-arg PIP_INDEX_URL=https://pypi.org/simple \
#                --build-arg PLAYWRIGHT_DOWNLOAD_HOST= ...
# ---------------------------------------------------------------------------
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# Chromium 下载源：默认官方（各架构文件完整）。npmmirror 等镜像可能缺失
# arm64 的 headless-shell 包导致 404，如使用镜像源请确认其完整性。
ARG PLAYWRIGHT_DOWNLOAD_HOST=

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Chromium 安装到公共路径，使非 root 运行用户也能读取
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
    # 转换内核与运行参数
    EXCEL2PDF_PY=/opt/excel2pdf/excel2pdf.py \
    SOFFICE=/usr/bin/soffice \
    CJK_FONT="Noto Sans CJK SC" \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

# ---------------------------------------------------------------------------
# 1. 系统依赖：LibreOffice(Calc + Writer) + poppler + 中文字体 + Chromium 运行库
#    --no-install-recommends 控制体积；同一层内清理 apt 缓存
# ---------------------------------------------------------------------------
# bookworm 使用 deb822 格式源文件；替换为镜像站后再更新
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-calc \
        libreoffice-core \
        libreoffice-writer \
        poppler-utils \
        fonts-noto-cjk \
        fontconfig \
        ca-certificates \
        # Chromium 运行所需的共享库（playwright --with-deps 会补齐其余）
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 \
        libatspi2.0-0 libx11-6 libxcb1 libxext6 libxi6 libxtst6 \
    && rm -rf /var/lib/apt/lists/*

# 字体缓存：必须在转换发生前构建，否则首次渲染可能取到不完整字体列表
RUN fc-cache -f -v >/dev/null 2>&1 || fc-cache -f

# ---------------------------------------------------------------------------
# 2. Python 依赖与 Chromium
# ---------------------------------------------------------------------------
WORKDIR /build
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX_URL}" -r requirements.txt \
    && if [ -n "${PLAYWRIGHT_DOWNLOAD_HOST}" ]; then \
         export PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST}"; \
       fi \
    && playwright install chromium \
    && playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# ---------------------------------------------------------------------------
# 3. 应用代码与只读转换内核
# ---------------------------------------------------------------------------
WORKDIR /app
COPY app ./app
# 既有内核以只读方式复用，不修改源文件
COPY excel2pdf.py /opt/excel2pdf/excel2pdf.py

# ---------------------------------------------------------------------------
# 4. 非 root 运行用户与数据目录
# ---------------------------------------------------------------------------
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/jobs /data/tmp \
    && chown -R appuser:appuser /data /app /opt/ms-playwright \
    && chmod -R a+rX /opt/excel2pdf

USER appuser
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000

# ---------------------------------------------------------------------------
# 5. 健康检查与启动
#    必须单 worker：任务队列与 Chromium 实例均在进程内，多进程会导致
#    job_id 查询落到错误进程而 404。
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--timeout-keep-alive", "120"]
