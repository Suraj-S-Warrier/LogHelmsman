FROM python:3.14-slim
WORKDIR /app
COPY worker_requirements.txt .
RUN pip install --no-cache-dir -r worker_requirements.txt
COPY worker.py .
CMD ["python","worker.py"]
