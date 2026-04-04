FROM python:3.14-slim
WORKDIR /app
COPY loadgen_requirements.txt .
RUN pip install --no-cache-dir -r loadgen_requirements.txt
COPY loadgen.py .
CMD ["python","loadgen.py"]
