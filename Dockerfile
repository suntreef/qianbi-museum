FROM python:3.12-slim

WORKDIR /app

# RapidOCR 的 opencv 运行需要的系统库
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 预下载 Indra 模型进镜像（构建环境可访问 HF）：运行时缓存命中、完全离线
ENV HF_HOME=/app/hf-cache
RUN python -c "import indra_ocr; indra_ocr.load(threads=2)"     || echo "[warn] Indra 模型预热失败——运行时会重试下载或降级 RapidOCR"

# 运行时也使用同一缓存路径，容器重建不丢模型
ENV HF_HOME=/app/hf-cache

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
