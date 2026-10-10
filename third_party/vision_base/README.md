# vision_base (vendored)

Vendored from https://github.com/Seeed-Solution/retail-vision at commit
`2af1ee5b54b3c5cdfef61eb74d8bccbf51c89bc3` (branch `wip/20261009-vision-base-stage2-clean`),
path `core-py/vision_base/`, Apache License 2.0 (`LICENSE` in this directory is the upstream file).

The package is Python standard library only (it includes its own MQTT 3.1.1 client).
`pyproject.toml` puts `third_party/` on the pytest path, so `import vision_base` works unchanged.
The upstream `tests/` directory is not copied.

Update by re-copying the `*.py` files from the upstream path and changing the commit above.
