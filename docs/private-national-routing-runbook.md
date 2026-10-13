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
- Historical repaired tile archive claim: 21,079,121,920 bytes, no SHA-256
- Portable bundle manifest archive: 20,887,162,880 bytes, SHA-256
  `0df937bf9044ab043e38b3844fcd65a41f9319389a9d40325497f5432412c846`
- Active `admins.sqlite`: 16,318,464 bytes
- Active `timezones.sqlite`: 122,478,592 bytes
- Fairway's merged private adapter pins modo commit
  `925312e48f4933512bf653ad58b005d4fba461ec`.

The image digest above is ARM64-specific. An x86-64 deployment needs a
separately resolved and recorded official `3.9.1` image digest. Do not replace
the digest with the mutable tag.

The two archive records conflict; their generation relationship is unresolved.
The portable bundle was materialized locally and its SHA-256 verified as
`faa1612082894e61f5bfe1c6172ddda578afd33f10cf469dc89ef3c15d67d05d`.
All included payload hashes matched its manifest, but the archive is omitted.
That verifies the manifest record, not the current Spark graph. Restore is
blocked until the actual intended graph archive is measured and hashed and
these records are reconciled. Do not choose a generation by size alone.
The launcher has an explicit hold before container creation; remove it only
in a reviewed change backed by that measurement and reconciliation.

## Supporting artifact status

The reviewed small-file subset is checked in at
[`ops/private-national-routing/`](../ops/private-national-routing/README.md).
It contains the exact active configuration, ARM64 launcher, download and limit
scripts, admin repair and rebuild inputs, PBF checksum sidecars, bounded
route/matrix verifier, and OSM attribution.

The repository intentionally excludes credentials, personal paths,
operational telemetry and logs, the live-installation fingerprint, historical
configuration backup, binary admin database, and unlabeled legacy checksum.
It also excludes the large PBF, repaired graph extract, expanded tiles, and
timezone database. Manifest-recorded sizes and SHA-256 identities are in the
checked-in operations README, but the archive conflict above remains open and
no off-machine runtime backup is claimed.
The multi-state admin/timezone validation is still not preserved as a
standalone script, so that remains a migration blocker.

## Resource evidence

These are measurements from this graph, not capacity promises.

| Phase | CPU | Memory | Disk/time |
| --- | --- | --- | --- |
| First national build | 12-core allocation | 80 GiB ceiling; 53.44 GiB measured peak | 95,715,598,000-byte peak data directory; 28m38s sampled lifecycle |
| Admin-aware rebuild | 12-core allocation | 43.07 GiB highest manual observation, not a sampled peak | 29m56s through archive and restart |
| Serving | CPU-only; current container capped at 12 CPUs | about 318 MiB fresh idle; about 1.415 GiB after one four-origin matrix, four routes, and metadata checks | historical 21,048,167,080-byte expanded tiles; archive generation unresolved |

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
`/custom_files`; the container restart policy is `unless-stopped`. Derive the
local rootless socket from the service user's `XDG_RUNTIME_DIR`, which must be
`/run/user/UID`. The scripts reject another endpoint or a `DOCKER_CONTEXT`
override and require rootless daemon `SecurityOptions` before mutations. Do not add
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
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR:?}/docker.sock"
docker start valhalla-us-261011
curl --fail --silent --show-error http://127.0.0.1:8003/status
VALHALLA_URL=http://127.0.0.1:8003 \
  ./ops/private-national-routing/verify-national-after-build.sh
```

After a host restart, first require the user `docker.service` to be active and
the container health state to become `healthy`. The verifier is the release
gate: it checks the bounded Seattle/New York/Miami/Los Angeles matrix and four
representative routes. Also retain the documented multi-state admin/timezone
check before redirecting an application. The verifier is now versioned, but
`/status` plus that bounded route check does not replace the missing standalone
metadata check.

## Backup and restore

Treat the dated national directory as one release. A recoverable backup
contains its launch/configuration and verification scripts plus the complete
`data/` directory, including the source and checksum files, active tiles and
archive, admin/timezone databases, repair inputs, and retained rollback copy.

1. Stop only `valhalla-us-261011` and confirm it is stopped.
2. Copy the release to a new backup path without following external links or
   overwriting an older backup. Preserve ownership and modes.
3. Create a SHA-256 manifest for the backup and save it beside the release.
4. Restart the original container and run the health check and checked-in
   national verifier.

After resolving the archive conflict above, restore into a new release
directory rather than overwriting the damaged one. Verify the saved manifest
and the pinned runtime/image identities before creating a replacement
container. The launcher requires nonempty config, PBF, tile archive and both
databases; a partial restore must fail rather than rebuild. Use
`VALHALLA_CONTAINER` and `VALHALLA_HOST_PORT` for an unused temporary name and
loopback port. A migration is
blocked until the checked-in verifier and the separate metadata checks pass;
then stop the old container and move the verified replacement to port 8003.
Keep the previous directory and container stopped until application smoke
tests pass. The `.pre-admin-repair` files are an
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
