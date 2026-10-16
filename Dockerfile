FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN python -m pip install --no-cache-dir .
ENV PROVIDER_MODE=mock
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
