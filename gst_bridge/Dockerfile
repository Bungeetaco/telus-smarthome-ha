FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-gi python3-aiohttp python3-boto3 python3-websocket \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
      gstreamer1.0-nice gir1.2-gst-plugins-bad-1.0 \
      ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# Stamped by compose (GIT_SHA build arg) so logs and /health say which commit
# runs; "unknown" when built by hand without it.
ARG GIT_SHA=unknown
ENV BRIDGE_VERSION=$GIT_SHA
LABEL org.opencontainers.image.revision=$GIT_SHA
WORKDIR /app
COPY telus_gst_bridge.py .
EXPOSE 8787
CMD ["python3", "telus_gst_bridge.py"]
