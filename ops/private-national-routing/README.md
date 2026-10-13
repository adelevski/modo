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

Large runtime assets are deliberately absent. The dated PBF identity below is
preserved from the portable bundle. The active archive, runtime configuration,
admin database and timezone database were verified read-only on Spark:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `us-261011.osm.pbf` | 12,199,919,524 | `de48784f65c1160eec691abf47821c7d8deab685c8cc984ba3720ce410c8b5df` |
| `valhalla_tiles.tar` | 21,079,121,920 | `0df937bf9044ab043e38b3844fcd65a41f9319389a9d40325497f5432412c846` |
| `admins.sqlite` | 16,318,464 | `73ebbdeaa7304d5b1a88d7a26a8ba191261ae7700b1baf5aea9f011f651baa98` |
| `timezones.sqlite` | 122,478,592 | `7861983249b9165f133f414145e4021fb3e05f18034458ef3b1987faac8a6b87` |

Read-only verification on 2026-10-13 confirmed the active archive above,
modified at 02:58:39 UTC. The rollback `valhalla_tiles.tar.pre-admin-repair`,
modified at 02:22:05 UTC, is 20,887,162,880 bytes with SHA-256
`6ba78841dd1e8d3f62644a407cf79cf7c2db2d14ce669f9062c1de10de6d1c24`.
Both files remained unchanged during hashing. The portable manifest mixed
the rollback size with the active archive hash; use the verified active
identity above for restore. The launcher rejects the rollback generation.

From this directory, verify the PBF before using the launcher:

```sh
cd data
md5sum --check us-261011.osm.pbf.md5
sha256sum --check us-261011.osm.pbf.sha256
```

The launcher requires a normal user, the local rootless socket under that
user's `/run/user/UID` directory, and rootless daemon `SecurityOptions`. It
refuses a different `DOCKER_HOST` or a `DOCKER_CONTEXT` override. It requires
the complete nonempty runtime configuration, PBF, archive, repaired admin
database and timezone database, verifies their recorded identities, and
disables admin/timezone/archive builds. It refuses non-ARM64 hosts and checks
the image digest. `ROUTING_DATA_DIR` defaults to the bundle-local `data/`
directory; `VALHALLA_HOST_PORT` and `VALHALLA_CONTAINER` select an unused
loopback-only port and temporary container name for replacement verification.
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
run it as a verification step. Before stopping anything it verifies the local
rootless daemon and reads the admin database in read-only mode, refusing an
existing US parent or a database without state rows. The SQL also skips parent
insertion if a US parent already exists or there are no state rows.
