FROM python:3.12-slim

ARG VERSION=dev
ARG SOURCE_URL=""
LABEL org.opencontainers.image.title="ZapConvert media worker" \
      org.opencontainers.image.description="ffmpeg, pdf2docx and PDF-to-Excel conversions for ZapConvert. Files are processed in a tmpfs and deleted after each job." \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.vendor="TeaGee Labs"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir pdf2docx openpyxl

WORKDIR /app
COPY worker.py pdf_xlsx.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "worker.py"]
