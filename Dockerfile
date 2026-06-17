FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# Set environment variables to avoid python buffering, which can delay logs
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY src/ /app/src/

# Run the agent
CMD ["python", "src/main.py"]
