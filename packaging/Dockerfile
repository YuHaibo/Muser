FROM ubuntu:24.04
# On arm64 hosts the Ubuntu cross driver invokes bare `as`/`ld`, while the
# package installs only the aarch64-prefixed names. Alias them for compiling.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl make g++-aarch64-linux-gnu python3 python3-pip \
    && ln -sf /usr/bin/aarch64-linux-gnu-as /usr/bin/as \
    && ln -sf /usr/bin/aarch64-linux-gnu-ld /usr/bin/ld \
    && pip3 install --break-system-packages cmake==3.31.6 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
