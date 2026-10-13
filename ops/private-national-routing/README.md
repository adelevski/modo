# Private national Valhalla files

This directory contains the reviewed, sanitized small-file subset for the
private, traffic-free United States Valhalla service. It contains no
credentials, personal paths, operational logs or telemetry, live-installation
fingerprint, historical configuration backup, binary database, graph, PBF, or
timezone database.

The default launch publishes container port 8002 only on host loopback port
8003. Do not broaden that binding. The image digest is Linux ARM64-specific;
resolve and review a separate immutable digest before any x86-64 test.

The checked-in files are:

- the exact active `data/valhalla.json` configuration;
- the dated PBF checksum sidecars and public Geofabrik download script;
- the ARM64 rootless-Docker launcher;
- the national-limit and admin-repair maintenance inputs; and
- the bounded route/matrix verifier and OSM attribution.

Large runtime assets are deliberately absent. Supplying files with the exact
identities below is required before launch or restore:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `us-261011.osm.pbf` | 12,199,919,524 | `de48784f65c1160eec691abf47821c7d8deab685c8cc984ba3720ce410c8b5df` |
| `valhalla_tiles.tar` | 20,887,162,880 | `0df937bf9044ab043e38b3844fcd65a41f9319389a9d40325497f5432412c846` |
| `timezones.sqlite` | 122,478,592 | `7861983249b9165f133f414145e4021fb3e05f18034458ef3b1987faac8a6b87` |

From this directory, verify the PBF before using the launcher:

```sh
cd data
md5sum --check us-261011.osm.pbf.md5
sha256sum --check us-261011.osm.pbf.sha256
```

The launcher requires rootless Docker and a normal user. It refuses non-ARM64
hosts, checks the image and PBF identities, and defaults to the bundle-local
`data/` directory. `ROUTING_DATA_DIR` may select a restored data directory and
`VALHALLA_HOST_PORT` may select another loopback-only port for verification.
Running it changes container state, so review the runbook and obtain operating
approval first.

After the private service is healthy:

```sh
VALHALLA_URL=http://127.0.0.1:8003 ./verify-national-after-build.sh
```

The verifier uses public-coordinate fixtures and makes one bounded 4x3 matrix
plus four detailed route requests. The admin rebuild script is destructive
maintenance: it stops the named container, preserves local rollback files,
repairs the admin database, rebuilds tiles, and restarts the container. Do not
run it as a verification step.
