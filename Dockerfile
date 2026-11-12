FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY static ./static
RUN useradd -u 10001 -m bridge && mkdir -p /data && chown bridge /data
USER bridge
ENV BRIDGE_DATA=/data BRIDGE_HOST=0.0.0.0 BRIDGE_PORT=8770
EXPOSE 8770
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8770/healthz')"
CMD ["python", "server.py"]
