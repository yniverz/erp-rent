FROM python:3.11-slim

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install system dependencies for lxml/Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libjpeg62-turbo-dev \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application files
COPY . .

# Create instance directory for SQLite database
RUN mkdir -p instance

# Expose port
EXPOSE 5000

# Run with gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
