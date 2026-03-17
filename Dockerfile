# Multi-stage Dockerfile for UpDown Bot
# Optimized for small image size

# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Runtime image
FROM python:3.12-slim AS runtime

WORKDIR /app

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash botuser

# Copy installed packages from builder
COPY --from=builder /root/.local /home/botuser/.local

# Copy application code
COPY updown_bot.py .

# Set ownership
RUN chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Add .local/bin to PATH
ENV PATH=/home/botuser/.local/bin:$PATH

# Run the bot
CMD ["python", "updown_bot.py"]
