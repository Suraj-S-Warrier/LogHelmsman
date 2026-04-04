FROM python:3.14-slim
WORKDIR /app
COPY frontend_requirements.txt .
RUN pip install --no-cache-dir -r frontend_requirements.txt
COPY frontend.py .
CMD ["python","frontend.py"]
