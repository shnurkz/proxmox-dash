# Use Red Hat Universal Base Image 9 with Python 3.12
FROM registry.access.redhat.com/ubi9/python-312:latest

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and the offline wheel files directory
COPY requirements.txt ./
COPY wheels/ ./wheels/

# Install Python dependencies offline from local wheels
RUN pip install --no-cache-dir --no-index --find-links=./wheels -r requirements.txt

# Copy application files and assets
COPY main.py ./
COPY static/ ./static/
COPY templates/ ./templates/

# Expose FastAPI default port
EXPOSE 8000

# Set the uvicorn launch command
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
