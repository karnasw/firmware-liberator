# Build environment for the lcgamboa QEMU fork (the ESP32-C3 and the classic
# ESP32, with Wi-Fi), so its
# build tooling stays out of the host. The binary it builds runs on Ubuntu 24.04
# and the distributions built on it.
#
#   docker build -t autoflash-qemu-build -f tools/espressif/emulator/qemu-build.Dockerfile tools/espressif/emulator
#   docker run --rm -u "$(id -u):$(id -g)" -v /path/to/lcgamboa-qemu:/src autoflash-qemu-build
FROM ubuntu:24.04

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential ninja-build pkg-config python3 python3-venv git ca-certificates \
        libglib2.0-dev libpixman-1-dev libslirp-dev libgcrypt20-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# The two system emulators the image has (emu.Dockerfile): RISC-V for the
# ESP32-C3, Xtensa for the classic ESP32. The shared-library step of the fork's
# own build script is for PICSimLab and not needed here.
CMD ["sh", "-c", "mkdir -p build && cd build && ../configure --target-list=riscv32-softmmu,xtensa-softmmu \
     --enable-slirp --enable-gcrypt --disable-werror --disable-docs --disable-gtk --disable-sdl \
     --disable-opengl --disable-vnc && ninja qemu-system-riscv32 qemu-system-xtensa"]
