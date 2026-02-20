FROM python:3.9-slim

WORKDIR /app

# Install system dependencies for cryptography and compiling
RUN apt-get update && apt-get install -y build-essential libssl-dev libffi-dev

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Changed: Removed --threads 4, kept -w 1. Added --timeout to be safe.
CMD python -c "import app; app.init_db()" && gunicorn -w 1 -b 0.0.0.0:5000 --timeout 120 app:app
