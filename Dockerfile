# edgebridge-aeb -- multi-arch image (linux/amd64, linux/arm64)
#
# python:3.12-slim publishes a multi-arch manifest, and cryptography / paho-mqtt / requests
# all ship prebuilt wheels for amd64 + arm64, so no compiler/Rust toolchain is needed here.
# This keeps QEMU cross-builds in CI fast and reliable.
#   - Synology NAS (Intel/AMD = amd64, modern ARM models = arm64)
#   - Raspberry Pi OS 64-bit (Pi 3 / 4 / 5 / Zero 2 W)
# For 32-bit ARM (armv7) see the README -- build locally with build tooling.
FROM python:3.12-slim

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY edgebridge.py .
COPY edgebridge.cfg .

# Persist registrations / redirects / callbacks / mqtt certs outside the container.
ENV EB_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8088

CMD ["python", "./edgebridge.py"]
