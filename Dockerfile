FROM python:3.11-slim

LABEL maintainer="Ioannis Kokkinis"
LABEL description="Sonarr YouTube Download Client - qBittorrent-compatible API for yt-dlp"

# Install ffmpeg
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install yt-dlp
RUN pip install --no-cache-dir yt-dlp

# Create app directory
WORKDIR /app

# Copy application
COPY ytdl_client.py /app/

# Create download directory
RUN mkdir -p /downloads

# Expose port
EXPOSE 8181

# Environment variables for configuration
ENV YTDL_HOST=0.0.0.0
ENV YTDL_PORT=8181
ENV YTDL_USERNAME=admin
ENV YTDL_PASSWORD=adminadmin
ENV YTDL_DOWNLOAD_PATH=/downloads

CMD ["python", "-u", "ytdl_client.py"]
