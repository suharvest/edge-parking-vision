# Jetson native parking image

This directory describes a local, unpushed Jetson build. The runtime is split
into two images:

1. `localhost/vb-base-jetson:local` is built from `retail-vision/docker/base-jetson.Dockerfile`.
   Its input is an artifact image containing a `vb-runtime` compiled on a
   Jetson with the exact CUDA/TensorRT/GStreamer SDK used by that host.
2. The parking image is built here and contains only `core_parking`. NRD-2,
   NRD-3 and NRD-4 share it; the mounted `vb.config/1` JSON selects the
   backend, model and business preset.

The base build must record the artifact source snapshot and the SHA256 of
`/opt/vb/bin/vb-runtime`. A local image ID is evidence for this workstation;
it is not a registry digest. Do not push, add a lock entry, or mark an image
verified from this workflow.

The 2026-10-05 local validation used the JPEG-enabled TRT-only artifact from
the Jetson Orin Nano SDK workspace. Its binary SHA256 was
`4167fd09f08a1d12e76c3b2db492d045ef5798e8d773dd35ad8ee6bf192833c1` and the
source snapshot archive SHA256 was
`9a5606f4aeca813c6be634ccde03beb00d4485fadca1bc7544115c5a95987ea0`.
The binary reports `0.1.0 tls:off`; the CMake cache requested TLS but the
SDK build did not link OpenSSL, so this local artifact is not evidence of TLS
support.

## Local build inputs

The SDK-side build must enable the native TensorRT backend with GStreamer:

```sh
cmake -S core-cpp/vb -B /tmp/vb-jetson-out \
  -DCMAKE_BUILD_TYPE=Release -DVB_TESTS=OFF \
  -DVB_BACKENDS=trt -DVB_WITH_GST=ON -DVB_WITH_JPEG=ON -DVB_WITH_TLS=ON \
  -DTRT_ROOT=/usr
cmake --build /tmp/vb-jetson-out -j"$(nproc)"
sha256sum /tmp/vb-jetson-out/vb-runtime
```

That command must run where `NvInfer.h`, `libnvinfer.so`, CUDA and the
Jetson GStreamer/NVDEC plugins are installed. The resulting binary is copied
to `/opt/vb/bin/vb-runtime` in a local artifact image; no SDK or model is
redistributed by this repository. The final base image build is:

```sh
docker build \
  --build-arg ARTIFACT_IMAGE=localhost/vb-jetson-artifact:local \
  --build-arg VB_RUNTIME_SHA256=<verified-binary-sha256> \
  -f <retail-vision>/docker/base-jetson.Dockerfile \
  -t localhost/vb-base-jetson:local <retail-vision>
docker build --build-arg BASE=localhost/vb-base-jetson:local \
  -f platforms/docker/Dockerfile -t edge-parking-jetson:local .
```

## Runtime contract

`docker-compose.jetson.yml` requires `PARKING_CONFIG` and
`PARKING_MODELS_DIR`; models are host mounts and are not copied into the
image. The host must provide the TensorRT/CUDA/NVDEC libraries listed in the
compose file, `/dev`, the NVIDIA container runtime, and a working GStreamer
`nvv4l2decoder`/`nvvidconv` plugin set. Set `PARKING_HEALTH_PORT` to the
`health.port` in the mounted `vb.config/1` JSON (default `8099`). The health
endpoint reports `503 degraded` while the external MQTT broker is unavailable.
The broker is external and is only
addressed by the mounted config.

Before any remote run, check SSH reachability, free disk, existing containers,
and the target's CUDA/TRT/NVDEC versions. Use a new container name and ports;
do not stop existing workloads. A container smoke test is incomplete until
the target device has demonstrated CUDA initialization, NVDEC pipeline
startup, IPC, and `/healthz` with the selected model.
