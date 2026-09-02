# Build context is the bridge/ directory (one up), because the daemon
# shares core.py with every other adapter (#425):
#   docker build -f yamcs/Dockerfile .
# The compose example next to this file is already wired that way.
FROM python:3.12-slim
RUN pip install --no-cache-dir requests websocket-client
WORKDIR /app
COPY core.py bridge/core.py
COPY yamcs/bridge.py bridge/yamcs/bridge.py
USER nobody
CMD ["python", "-u", "bridge/yamcs/bridge.py"]
