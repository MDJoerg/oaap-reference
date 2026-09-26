FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# One file, listed by name like every other image here -- a new module
# MUST be added to this line (CURRENT_STATE 132).
COPY app.py ./
# the laptop client, offered for download at /connect/client (spec 2.8.5)
COPY client/oaap-expose.py ./client/oaap-expose.py
EXPOSE 8000
CMD ["python", "-u", "app.py"]
