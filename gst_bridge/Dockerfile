# Pinned by digest (Debian stable point releases are the pin; per-package
# versions would break on the next security update). Refresh with:
#   docker buildx imagetools inspect debian:bookworm-slim --format '{{.Manifest.Digest}}'
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
# gstreamer1.0-libav is NOT in the data path (opusdec is in -base, the RTP
# depayloaders in -good, webrtcbin in -bad) and pulled ~150 MB of codecs;
# gstreamer1.0-plugins-ugly stays only for x264enc, which tests/ use to
# synthesise an RTP source.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-gi python3-aiohttp python3-boto3 python3-websocket \
      gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
      gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
      gstreamer1.0-nice gir1.2-gst-plugins-bad-1.0 \
      ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
# The container runs read-only as an unprivileged user (compose); GStreamer's
# plugin registry and Python's caches go to the /tmp tmpfs.
ENV GST_REGISTRY=/tmp/gst-registry.bin HOME=/tmp XDG_CACHE_HOME=/tmp PYTHONDONTWRITEBYTECODE=1
# Stamped by compose (GIT_SHA build arg) so logs and /health say which commit
# runs; "unknown" when built by hand without it.
ARG GIT_SHA=unknown
ENV BRIDGE_VERSION=$GIT_SHA
LABEL org.opencontainers.image.revision=$GIT_SHA
WORKDIR /app
COPY telus_gst_bridge.py .
EXPOSE 8787
CMD ["python3", "telus_gst_bridge.py"]
