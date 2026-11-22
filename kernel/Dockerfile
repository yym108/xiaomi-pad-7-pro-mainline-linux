FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    wget \
    git \
    build-essential \
    bc \
    bison \
    flex \
    libssl-dev \
    libelf-dev \
    libncurses-dev \
    dwarves \
    kmod \
    cpio \
    gzip \
    xz-utils \
    lz4 \
    zstd \
    device-tree-compiler \
    python3 \
    mkbootimg \
    busybox-static \
    android-sdk-libsparse-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
