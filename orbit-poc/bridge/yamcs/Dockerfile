FROM python:3.12-slim
RUN pip install --no-cache-dir requests
COPY bridge.py /app/bridge.py
USER nobody
CMD ["python", "-u", "/app/bridge.py"]
