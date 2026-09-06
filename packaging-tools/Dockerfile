ARG BUILD_IMAGE
FROM ${BUILD_IMAGE}
COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ARG SNAPSHOT
RUN printf 'deb https://snapshot.ubuntu.com/ubuntu/%s jammy main universe\ndeb https://snapshot.ubuntu.com/ubuntu/%s jammy-updates main universe\ndeb https://snapshot.ubuntu.com/ubuntu/%s jammy-security main universe\ndeb-src https://snapshot.ubuntu.com/ubuntu/%s jammy main universe\ndeb-src https://snapshot.ubuntu.com/ubuntu/%s jammy-updates main universe\ndeb-src https://snapshot.ubuntu.com/ubuntu/%s jammy-security main universe\n' "$SNAPSHOT" "$SNAPSHOT" "$SNAPSHOT" "$SNAPSHOT" "$SNAPSHOT" "$SNAPSHOT" > /etc/apt/sources.list && \
    apt-get -o Acquire::Retries=3 -o Acquire::Check-Valid-Until=false update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates build-essential git cmake ninja-build pkg-config python3 \
      autoconf automake libtool meson gettext texinfo bison flex xz-utils file \
      curl wget clang libmagic-dev libjpeg-dev libpng-dev cimg-dev \
      libgpgme-dev libgcrypt20-dev libglib2.0-dev libcurl4-openssl-dev \
      libssl-dev libzstd-dev zlib1g-dev libnghttp2-dev libidn2-dev libpsl-dev \
      libbrotli-dev libc-ares-dev libunistring-dev liblzma-dev libffi-dev \
      libselinux1-dev libsepol-dev libmount-dev libblkid-dev libpcre2-dev \
      libmimalloc-dev libudev-dev desktop-file-utils squashfs-tools zsync && \
    mkdir -p /sources /work /out
COPY build.sh inputs.json Dockerfile /build/
ENTRYPOINT ["/bin/bash", "/build/build.sh"]
