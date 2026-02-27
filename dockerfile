# Use a lightweight, official Python runtime
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project files
COPY . .

# Expose the standard Cloud Run port
EXPOSE 8080

# Command to run the LangGraph orchestrator via a standard web server
CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:8080", "--timeout", "300"]