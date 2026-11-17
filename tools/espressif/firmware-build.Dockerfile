# What builds the firmware of this project's that is not ESPHome: the bridge
# (archive/bridge) and the recovery Safeboot (tools/espressif/safeboot), with
# PlatformIO, which downloads its own toolchains into /root/.platformio
# (a volume: tools/repo/update_firmware.py esp32).
FROM python:3.12.11-slim-bookworm
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        git cmake ninja-build ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir platformio==6.1.19
