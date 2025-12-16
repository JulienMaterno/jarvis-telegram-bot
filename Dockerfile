FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main_webhook.py .

# Run with webhook mode
CMD ["python", "main_webhook.py"]
