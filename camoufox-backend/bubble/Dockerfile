FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates python3 python3-venv python3-pip \
      libgtk-3-0 libpangocairo-1.0-0 libpango-1.0-0 libatk1.0-0 \
      libatk-bridge2.0-0 libatspi2.0-0 libcairo-gobject2 libcairo2 \
      libgdk-pixbuf-2.0-0 libcups2 libdbus-1-3 libasound2 libnss3 \
      libnss3-tools libnspr4 libdrm2 libxkbcommon0 libgbm1 libxshmfence1 \
      libxcb-shm0 libx11-xcb1 libx11-6 libxcb1 libxext6 libxrandr2 \
      libxcomposite1 libxcursor1 libxdamage1 libxfixes3 libxi6 libxrender1 \
      libfreetype6 libfontconfig1 dbus-x11 \
      fonts-noto-color-emoji fonts-noto-cjk fonts-freefont-ttf \
      xvfb x11-xserver-utils xfonts-base x11vnc websockify novnc \
    && rm -rf /var/lib/apt/lists/*

COPY camoufox-backend/requirements.txt /worker/requirements.txt
COPY camoufox-backend/*.py /worker/
COPY camoufox-backend/gestures/ /worker/gestures/

RUN python3 /worker/bootstrap.py --runtime-dir /opt/agent-browser-runtime

EXPOSE 6080 5900

ENTRYPOINT ["/opt/agent-browser-runtime/venv/bin/python", "/worker/worker.py"]
CMD ["--runtime-dir", "/opt/agent-browser-runtime", "--motion", "fast"]