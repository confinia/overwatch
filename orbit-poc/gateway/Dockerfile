FROM docker.io/library/python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway.py .
EXPOSE 8088
# -u: unbuffered, so the rate/cooldown log lines reach podman logs immediately.
CMD ["python", "-u", "gateway.py"]
