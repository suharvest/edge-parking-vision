# syntax=docker/dockerfile:1
# Native Jetson parking application. The base supplies vb-runtime and
# vision_base; this image contains only the stdlib parking event layer.
# Models, configs, broker and state are runtime mounts.
ARG BASE=localhost/vb-base-jetson:local
FROM ${BASE}

WORKDIR /opt/edge-parking
COPY core-py/core_parking ./core_parking

ENV PYTHONPATH=/opt/vb/py:/opt/edge-parking \
    VB_PRODUCTION=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# vision_base.main remains the single process entrypoint. The config path is
# supplied by compose so NRD-2/3/4 can share this image and vary only JSON.
