FROM python:3.14-slim
WORKDIR /app
COPY backend_requirements.txt .
RUN pip install --no-cache-dir -r backend_requirements.txt
COPY backend.py .
CMD ["python","backend.py"]
