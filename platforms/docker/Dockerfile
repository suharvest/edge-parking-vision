# syntax=docker/dockerfile:1
# Native Jetson parking application. The base supplies vb-runtime; this image
# adds the stdlib parking event layer and the vendored vision_base
# (third_party/vision_base), which takes precedence over the base image's copy.
# Overlay images (tools/build_overlay_images.sh) keep the base image's own
# vision_base instead; equivalence of the two copies has not been verified.
# Models, configs, broker and state are runtime mounts.
ARG BASE=localhost/vb-base-jetson:local
FROM ${BASE}

WORKDIR /opt/edge-parking
COPY core-py/core_parking ./core_parking
COPY third_party/vision_base ./vision_base

ENV PYTHONPATH=/opt/edge-parking:/opt/vb/py \
    VB_PRODUCTION=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# vision_base.main remains the single process entrypoint. The config path is
# supplied by compose so counting, plate and slots can share this image and vary only JSON.
