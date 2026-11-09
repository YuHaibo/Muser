#!/bin/sh
set -eu
MUSER_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
MUSER_BUILD_IMAGE=${MUSER_BUILD_IMAGE:-muser-build:ubuntu24.04}
docker build -t "$MUSER_BUILD_IMAGE" -f "$MUSER_PROJECT_DIR/packaging/Dockerfile" "$MUSER_PROJECT_DIR/packaging"
if [ -n "${MUSER_BSP_DIR:-}" ]; then
    docker run --rm -v "$MUSER_PROJECT_DIR:/work" -v "$MUSER_BSP_DIR:/bsp:ro" -w /work "$MUSER_BUILD_IMAGE" sh -c 'cmake --preset cp0-cross -DCM0_SDK_ROOT=/bsp && cmake --build --preset cp0-cross-rel -j4 && cpack --preset cp0-cross-deb'
else
    docker run --rm -v "$MUSER_PROJECT_DIR:/work" -w /work "$MUSER_BUILD_IMAGE" cmake --workflow --preset cp0-cross-package
fi
