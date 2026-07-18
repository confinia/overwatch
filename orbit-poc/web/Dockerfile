FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask==3.0.3 psycopg2-binary==2.9.9 gunicorn==23.0.0
COPY app.py .
COPY static ./static
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "2", "app:app"]
