FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# One file, listed by name like every other image here -- a new module
# MUST be added to this line (CURRENT_STATE 132).
COPY app.py ./
EXPOSE 8000
CMD ["python", "-u", "app.py"]
