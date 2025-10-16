FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY event_listener.py .

# Create non-root user
RUN useradd -m -u 1000 listener && \
    chown -R listener:listener /app

USER listener

CMD ["python", "event_listener.py"]
