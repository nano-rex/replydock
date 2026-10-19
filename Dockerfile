FROM python:3.12-slim
WORKDIR /app
COPY server.py ./
COPY static ./static
ENV REPLYDOCK_HOST=0.0.0.0 REPLYDOCK_PORT=8080 REPLYDOCK_DB=/data/replydock.sqlite3 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python3", "server.py"]
