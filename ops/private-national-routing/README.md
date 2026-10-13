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
- the canonical `data/generation-manifest.json` and read-only runtime verifier;
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

`data/generation-manifest.json` is the canonical machine-readable release
record for the image/platform, configuration and runtime file identities.
The launcher reads its sizes and SHA-256 values through
`verify-runtime-files.py`; it refuses missing, empty, linked, changed or
mismatched assets before creating a container. `verified-identities` records
the established pins, not migration, load or metadata-validation readiness.
Private host inventory should reference the modo commit and this manifest;
host paths, credentials and application policy belong to their own operators.

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
plus four detailed route requests.

The admin rebuild script stages maintenance in a new generation. It verifies
the local rootless daemon and reads the source admin database in read-only
mode, refusing an existing US parent or a database without state rows. It
copies the small inputs and snapshots the admin DB into a unique directory
under `ROUTING_STAGING_ROOT` (default: the source data directory's parent).
Builders have no network, write only that generation, and mount the source
PBF read-only. The existing data and routing containers are never stopped,
renamed or switched. Failures retain the staged directory; normal exit and
handled signals attempt bounded cleanup of only this build's recorded
container IDs. Client termination waits up to two seconds after TERM, escalates
to KILL, then waits up to one second before reporting a surviving client. Each
container removal times out after ten seconds and escalates to KILL two seconds
later. SIGKILL or Docker daemon failure still needs operator cleanup.

Successful staging writes `generation-manifest.json` with status
`staged-unverified`; the source PBF is referenced but not copied. This status
cannot pass the launcher gate. Supply the verified PBF, validate admin/timezone
metadata and bounded routes/matrices, then review and pin the complete new
release before a separately approved replacement/switch. The helper does
not promote it. Build resource/disk approval is required even though serving
data is preserved. Do not run it as a verification step.
