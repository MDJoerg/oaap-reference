FROM python:3.12-slim
WORKDIR /app
# Build context is services/, not services/identity -- see
# docker-compose.yml. place.py is shared with the portal on purpose.
COPY identity/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY place.py .
COPY identity/app.py .
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "app:app"]
