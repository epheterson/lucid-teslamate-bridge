FROM python:3.12-slim

# grpcio wheels exist for slim; no build toolchain needed.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mapping.py app.py ./

# Never run as root: this process holds the only Lucid credential in the
# stack, mounted read-only at /secrets.
RUN useradd -r -u 10001 bridge
USER bridge

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=8).status==200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
