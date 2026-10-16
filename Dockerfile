FROM python:3.11-slim

WORKDIR /app

# Install system deps (optional — Termux:API not available in cloud)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py run_prod.py ./

# Persistent volume mount point (Fly.io/Railway will attach here)
ENV DB_FILE=/data/termux_gui.db
VOLUME ["/data"]

EXPOSE 5000
CMD ["python", "run_prod.py"]
