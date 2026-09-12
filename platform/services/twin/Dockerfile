FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Files are listed one by one, not 'COPY . .' -- a new module MUST be added
# here (the portal's own restart loop over a missing twin_view.py, CURRENT_
# STATE 132, is the reason this comment exists). relay.py is the same image
# run with a different command by the 'relay' service (oaap.data.twin 0.3).
COPY app.py relay.py ./
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "app:app"]
