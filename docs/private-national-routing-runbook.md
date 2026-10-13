# Private national routing runbook

This is the portable operating note for the current private national routing
slice. It does not authorize a public endpoint, a Render migration, new
credentials, or a graph rebuild. Valhalla is traffic-unaware and CPU-only.
modo's national result remains a bounded approximation.

## Pinned release

- Valhalla: `3.9.1`
- Current ARM64 image:
  `ghcr.io/valhalla/valhalla-scripted:3.9.1@sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6`
- OSM source: Geofabrik `us-261011.osm.pbf`, 12,199,919,524 bytes
- Source SHA-256:
  `de48784f65c1160eec691abf47821c7d8deab685c8cc984ba3720ce410c8b5df`
- Source MD5: `f9a56ae3ef76583cf892e5bfe23a18d4`
- Active repaired tile archive: 21,079,121,920 bytes
- Active `admins.sqlite`: 16,318,464 bytes
- Active `timezones.sqlite`: 122,478,592 bytes
- Fairway's merged private adapter pins modo commit
  `925312e48f4933512bf653ad58b005d4fba461ec`.

The image digest above is ARM64-specific. An x86-64 deployment needs a
separately resolved and recorded official `3.9.1` image digest. Do not replace
the digest with the mutable tag.

## Supporting artifact status

This modo repository currently checks in the runbook, not the operational
routing bundle. The following inspected files exist in the unversioned
`spark-valhalla-illinois/` directory from Codex task
`01a132bb-516f-74f8-83cd-adb06e2a30d4`; that task directory is not a durable
deployment artifact. The launch, admin repair, rebuild, and
limit-configuration files also exist under
`/home/adelevski/routing/valhalla-us-261011/` on the current host; the verifier
does not.

| Purpose | Task-artifact path | SHA-256 |
| --- | --- | --- |
| ARM64 launch | `national/launch-us-261011.sh` | `f459249c3ce6f4302319f04d684cdbfdf1fce242ce3f2387ae1654953f1b13f3` |
| Bounded verifier | `scripts/verify-national-after-build.sh` | `d9cdb80b870392ae0669f9005642bf5a42366bbc53309ef9b25dda0923b2aed3` |
| Admin repair | `national/repair-us-admin-parent.sql` | `e440717553345d6a7760a7b4672118fd87076ac773d32822a87c4755858c7f66` |
| Rebuild | `national/rebuild-with-repaired-admins.sh` | `1201e372c01fb5ad6be35f25a31d7ee32ea07ccb66372ac3e8d1b08f338f3ba9` |
| Limit configuration | `national/configure-national-limits.py` | `85825c80a97fe6b957bd545a7a42d9bc12caf82ee86d88f9583b23964093267f` |
| PBF download | `national/download-us-261011.sh` | `488ac4feebf7aba3890c10c2e2cca68263797a48c39fb279138dfd430a4b2e5d` |
| Build sampler | `national/sample-build.sh` | `5f47d8efd9d7a192b9617298d81102a6e09099721274f7490d68114dd419911d` |

The active Spark `data/valhalla.json` has SHA-256
`0ba4757cdc8f61be80953ecaec7eb7500513e82febc24d1551140b66891dee83`,
but it is not in Git or a verified portable artifact. The verifier is also not
installed in the live release, and the multi-state admin/timezone validation
was not preserved as a standalone script.

Therefore this runbook can describe and operate the existing installation but
does not by itself enable clean recreation. Before migration, publish one
checksummed, access-controlled routing bundle containing the files above, the
exact active configuration, a standalone metadata verifier, and either the
verified tile release plus manifest or all inputs needed to rebuild it. Record
its durable artifact identifier here. No such identifier exists yet.

## Resource evidence

These are measurements from this graph, not capacity promises.

| Phase | CPU | Memory | Disk/time |
| --- | --- | --- | --- |
| First national build | 12-core allocation | 80 GiB ceiling; 53.44 GiB measured peak | 95,715,598,000-byte peak data directory; 28m38s sampled lifecycle |
| Admin-aware rebuild | 12-core allocation | 43.07 GiB highest manual observation, not a sampled peak | 29m56s through archive and restart |
| Serving | CPU-only; current container capped at 12 CPUs | about 318 MiB fresh idle; about 1.415 GiB after one four-origin matrix, four routes, and metadata checks | 21,048,167,080-byte expanded tiles plus the archive above |

The current 12-CPU and 80-GiB container limits preserve the measured build
envelope; they are not measured serving requirements. A 64-GiB build host is
only an untested lower candidate and leaves limited headroom above the observed
53.44-GiB process peak before host and container overhead. For a comfortable
repeat build, plan 96-128 GiB RAM, 12 capable CPU cores, and 250 GB free disk as
a conservative working allowance. Serving CPU saturation, concurrency, modo
API resources, and Fairway API resources have not been measured. Remeasure
them before reducing limits or claiming production capacity.

## Private topology and calls

Rootless Docker binds container port 8002 only to host
`127.0.0.1:8003`. The persistent data directory is bind-mounted at
`/custom_files`; the container restart policy is `unless-stopped`. Keep the
current rootless socket at `unix:///run/user/1000/docker.sock`. On another
host derive it from `XDG_RUNTIME_DIR` instead of assuming UID 1000. Do not add
a public, LAN, or Tailscale listener.

Both application processes use the same loopback base URL:

```python
router = ValhallaHTTPProvider("http://127.0.0.1:8003", timeout_seconds=10)
estimator = NationwideEstimator(router)
```

modo calls the estimator for a bounded candidate area and the provider for
small route or matrix requests. Fairway imports the pinned modo library in its
calculation process: `ModoPromisingAreaProvider(estimator)` obtains candidate
areas, its catalog forms a bounded nearby shortlist, and
`ModoMatrixProvider(router)` scores only that shortlist. Fairway does not call
a hosted modo service and neither application routes every facility.

If the applications move to separate containers, replace the loopback URL
with a private-container-network name supplied by ordinary service
configuration. Keep it out of application code and do not publish Valhalla.

## Restart and verification

Run as the normal service user, never as root:

```sh
export DOCKER_HOST=unix:///run/user/1000/docker.sock
docker start valhalla-us-261011
curl --fail --silent --show-error http://127.0.0.1:8003/status
```

After a host restart, first require the user `docker.service` to be active and
the container health state to become `healthy`. The verifier is the release
gate: it checks the bounded Seattle/New York/Miami/Los Angeles matrix and four
representative routes. Also retain the documented multi-state
admin/timezone check before redirecting an application. Run those checks from
the future versioned routing bundle; the current live release does not contain
the verifier, so `/status` alone is not a migration gate.

## Backup and restore

Treat the dated national directory as one release. A recoverable backup
contains its launch/configuration and verification scripts plus the complete
`data/` directory, including the source and checksum files, active tiles and
archive, admin/timezone databases, repair inputs, and retained rollback copy.

1. Stop only `valhalla-us-261011` and confirm it is stopped.
2. Copy the release to a new backup path without following external links or
   overwriting an older backup. Preserve ownership and modes.
3. Create a SHA-256 manifest for the backup and save it beside the release.
4. Restart the original container and run the health check. Once the routing
   bundle is published, run its national verifier too.

Restore into a new release directory rather than overwriting the damaged one.
Verify the saved manifest and the pinned PBF/image identities before creating
a replacement container. Launch it on an unused loopback port. A migration is
blocked until the published routing bundle's full verifier and metadata checks
pass; then stop the old container and move the verified replacement to port
8003. Keep the previous directory and container stopped until application
smoke tests pass. The `.pre-admin-repair` files are an
emergency rollback generation, not a substitute for a complete release
backup; they predate the verified admin repair.

## ARM64 to x86-64 gate

- Record `uname -m`, container runtime version, and available CPU, RAM, and
  disk on the destination.
- Resolve and pin the official Linux AMD64 digest for Valhalla `3.9.1`; the
  ARM64 digest above must not be reused.
- Restore the checksummed graph release without rebuilding it first. Preserve
  the source and old ARM64 release as rollback evidence.
- Bind only a temporary loopback port and require `/status` to report `3.9.1`.
- Run the national verifier and the multi-state admin/timezone checks. Treat
  any tile-format or routing difference as a failed portability test, not a
  reason to rebuild silently.
- Run the modo/Fairway Chicago coupled smoke and the four-corner bounded smoke.
  Confirm request counts and approximation metadata; do not require an
  eligible catalog destination where the catalog has none.
- Measure cold start, idle RSS, request RSS, and CPU for the bounded smoke on
  x86-64. No x86-64 serving measurement exists yet.
- Switch an application only after these checks pass, one application at a
  time. Keep the ARM64 service ready for rollback and leave Render unchanged
  until a separately reviewed retirement.
