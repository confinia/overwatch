FROM docker.io/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY statusmon.py healthz.py .
# -u: unbuffered, so probe failures reach podman logs immediately.
CMD ["python", "-u", "statusmon.py"]
