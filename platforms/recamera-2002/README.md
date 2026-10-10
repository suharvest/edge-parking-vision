# reCamera 2002 package preparation

This directory is the SG2002 plate preparation path. It contains no model weights
and packaging does not start or stop a device service.

`build-python-runtime.sh` stages CPython 3.11.17 under `/opt/vb/python` using
the supplied riscv64-musl toolchain. It uses an existing source tarball when
provided; otherwise it fetches only the approved Huawei mirror URL and checks
the official SHA-256. Missing target OpenSSL, libffi, zlib, or cross-build
support fails closed instead of disabling `_ssl`, `hashlib`, `socket`, or
compression modules.

`build-deb.sh` requires real RISC-V `vb-runtime` and Python staging. It copies
the vendored `third_party/vision_base` and `core_parking`, creates the K92 SysV script and control files,
and runs `dpkg-deb --build --root-owner-group`. Models stay outside the deb and
belong directly under `/userdata/local/models/` through the upstream preset.

Example:

```sh
./build-python-runtime.sh \
  --sysroot <toolchain>/riscv64-linux-musl-x86_64/sysroot \
  --cross-prefix <toolchain>/riscv64-linux-musl-x86_64/bin/riscv64-unknown-linux-musl- \
  --out /path/to/owned/python-runtime
./build-deb.sh --version 0.1.0 \
  --recamera <sg2002-recamera-sdk> \
  --python-staging /path/to/owned/python-runtime/stage \
  --vb-runtime /path/to/vb-runtime
```

The checked WSL SDK has the CVI `vb-runtime` crosslink but no Python 3.10+
target runtime or dependency staging, so these inputs must be built in an
owned output namespace before a deb can be produced. These files do not claim a
completed deb.

## Existing solution parameter mapping

The package keeps the existing `recamera_plate.yaml` contract:

- package/service: `edge-parking-plate` / `K92edge-parking-plate`
- legacy deployment parameters: `/etc/edge-parking-plate.conf`
- generated runtime config: `/etc/edge-parking-plate/parking.vb.config.json`
- external models: `/userdata/local/models/plate_det_yolox_tiny_416_cv181x_int8.cvimodel` and `/userdata/local/models/plate_rec_cn_96x32_cv181x_int8.cvimodel`

The K92 script reads the legacy file and renders the packaged `vb.config/1`
template using the Python standard library. Package installation
never starts or stops the service; appMgr invokes the K92 script.
