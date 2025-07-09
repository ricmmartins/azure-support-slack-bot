FROM python:3.13-slim

# Set workdir
WORKDIR /app

# Copy requirements and install dependencies
# COPY requirements.txt .
# RUN pip install --upgrade pip && pip install -r requirements.txt

# Copy the app
COPY . .

# Install dependencies
RUN pip install --upgrade pip && pip install -r requirements.txt

# NOTE: Workaround to use local credentials. NOT needed when deploying to Azure with user-assigned credentials
RUN pip install azure-cli

# Set environment variables (override as needed)
ENV PYTHONUNBUFFERED=1

# Expose port (if running web server)
EXPOSE 5000

# Default command
CMD ["python3", "app.py"]
