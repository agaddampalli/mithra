FROM python:3.12-slim

WORKDIR /app

# tesseract-ocr enables Kreuzberg OCR for scanned PDFs and images
# Without it: native PDFs, DOCX, XLSX, CSV still work
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY household.yaml /app/household.yaml
COPY services/ /app/services/

EXPOSE 8000

CMD ["uvicorn", "mithra.api.gateway:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
