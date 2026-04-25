FROM python:3.12-slim

# FFmpeg o'rnatish
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Ishchi papka
WORKDIR /app

# Dependencylarni o'rnatish
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bot kodini nusxalash
COPY bot.py .

# Downloads papkasini yaratish
RUN mkdir -p downloads

# Botni ishga tushirish
CMD ["python", "bot.py"]
