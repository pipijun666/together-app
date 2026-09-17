FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# CN mirrors so the image builds reproducibly and quickly on CN hosts
# (apt sources: deb822 /etc/apt/sources.list.d/debian.sources on trixie,
#  fallback /etc/apt/sources.list on older bases; failures are non-fatal).
RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.tuna.tsinghua.edu.cn/debian|g; s|http://security.debian.org/debian-security|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || sed -i 's|http://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt && useradd --create-home --uid 10001 together
COPY common.py link_input.py server.py worker.py storage.py storage_r2.py ./
COPY client ./client
RUN mkdir /data && chown together:together /data
USER together
ENV DATA_DIR=/data HOST=0.0.0.0 PORT=8765
EXPOSE 8765
CMD ["python", "server.py"]
