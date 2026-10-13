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

Large runtime assets are deliberately absent. The locally verified portable
bundle manifest records the identities below; its included admin database was
also verified against that manifest. The omitted graph bytes were not verified:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `us-261011.osm.pbf` | 12,199,919,524 | `de48784f65c1160eec691abf47821c7d8deab685c8cc984ba3720ce410c8b5df` |
| `valhalla_tiles.tar` | 20,887,162,880 | `0df937bf9044ab043e38b3844fcd65a41f9319389a9d40325497f5432412c846` |
| `admins.sqlite` | 16,318,464 | `73ebbdeaa7304d5b1a88d7a26a8ba191261ae7700b1baf5aea9f011f651baa98` |
| `timezones.sqlite` | 122,478,592 | `7861983249b9165f133f414145e4021fb3e05f18034458ef3b1987faac8a6b87` |

The runbook previously called a 21,079,121,920-byte archive the active repaired
generation, without a SHA-256. Its relationship to the manifest generation is
unresolved. Restore is blocked until the actual intended archive is measured
and hashed and that conflict is reconciled. Do not infer that either record
identifies the current Spark graph or substitute one archive for the other.
The launcher checks the manifest generation's exact archive size and SHA-256;
those checks alone do not establish which generation is active on Spark. An
explicit launch hold also refuses container creation until a reviewed change
reconciles the archive evidence and removes the hold. There is no runtime
override for this hold.

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
