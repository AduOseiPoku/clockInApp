FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (curl for container healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python packages
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy project code
COPY . /app/

# Make entrypoint script executable
RUN chmod +x /app/entrypoint.sh

EXPOSE 8000

# Container healthcheck for Dokploy/Docker
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health/ || exit 1

# Start via production entrypoint script (runs migrations, collectstatic, and gunicorn)
ENTRYPOINT ["/app/entrypoint.sh"]
