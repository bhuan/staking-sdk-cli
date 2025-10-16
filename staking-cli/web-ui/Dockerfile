FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .
COPY templates/ templates/

# Run as non-root user
RUN useradd -m -u 1000 webui && \
    chown -R webui:webui /app

USER webui

EXPOSE 5000

CMD ["python", "app.py"]
