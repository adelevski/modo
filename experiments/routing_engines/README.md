# Valhalla routing benchmark

This non-production experiment measures the local Valhalla primitives relevant
to modo and fairway. The benchmark itself does not change modo's dependencies,
run a service, or create traffic data. The optional correctness probe below uses
only isolated synthetic data.

For each case, the harness creates a fresh `Actor` and measures:

- Actor startup;
- the first route and repeated identical warm routes;
- one all-origins to all-targets matrix; and
- one polygon isochrone per origin.

The first origin and target are the default route pair. A case can select other
zero-based indexes with `route`. `date_time`, when present, is passed to every
request. It only affects routing when the supplied Valhalla graph contains the
corresponding traffic speeds. Time-dependent matrices also require a positive
`service_limits.max_timedep_distance_matrix`; generated configs default it to
zero. The baseline traffic archive is an empty skeleton, so this benchmark does
not claim live or historical traffic behavior.

## Run

This benchmark needs a locally built Valhalla graph, its generated config, and a
Python environment containing `pyvalhalla`. None of them are retained in this
repository, and the measurements below are the durable record of the run. Build
them locally first, then substitute your own paths for `BENCHMARK_DIR`.

From the modo repository root:

```sh
"$BENCHMARK_DIR"/.venv/bin/python \
  experiments/routing_engines/benchmark.py \
  --config "$BENCHMARK_DIR"/valhalla.json \
  --cases experiments/routing_engines/cases.example.json \
  --warm-runs 5 \
  --size graph=83517440 \
  --size traffic=7946240 \
  --size source-pbf=101631355 \
  > experiments/routing_engines/results/chicago.json
```

Create `results` first if saving output. It is ignored by Git. Omitting the
redirect prints JSON to standard output. Valhalla logs separately to standard
error. `--size NAME=BYTES` only records caller-supplied metadata and can be
repeated or omitted.

Output includes the engine and environment versions, configuration SHA-256,
input sizes, millisecond timings, warm samples and median, and process peak RSS
when the platform exposes it. Peak RSS is a lifetime high-water mark, not live
memory. Run one case per process for isolated memory comparisons.

Case files use public coordinates rather than addresses and follow this schema:

```json
{
  "schema": "modo-valhalla-cases-v1",
  "cases": [
    {
      "name": "example",
      "costing": "auto",
      "origins": [{"lat": 41.0, "lon": -87.0}, {"lat": 42.0, "lon": -88.0}],
      "targets": [{"lat": 41.5, "lon": -87.5}],
      "route": {"origin": 0, "target": 0},
      "isochrone_minutes": 15,
      "date_time": {"type": 1, "value": "2026-09-04T08:00"}
    }
  ]
}
```

Do not commit personal locations or benchmark results. Results describe only
the named graph, configuration, machine, and engine version.

## Chicago baseline

On September 4, 2026, Valhalla 3.8.3 on arm64 Python 3.12 loaded an 83,517,440
byte tile extract in 2.28 ms. One cold route took 299.21 ms; ten warm routes had
a 4.35 ms median. A 2 by 2 matrix took 53.17 ms, and two isochrones took 334.60
and 91.51 ms. Process peak RSS was 183,681,024 bytes. These are local
high-water measurements, not hosting guarantees.

Time-dependent requests require a real timezone database. The probe used the
2026c `timezones-with-oceans` release from timezone-boundary-builder, generated
Valhalla's Spatialite database, and rebuilt separate tiles. The same Chicago
route then resolved to `-06:00` in January and `-05:00` in September. A build
that warns it is using UTC is not a valid traffic experiment.

- [Valhalla timezone build script](https://github.com/valhalla/valhalla/blob/3.8.3/scripts/valhalla_build_timezones)
- [Timezone Boundary Builder releases](https://github.com/evansiroky/timezone-boundary-builder/releases)

## Historical traffic correctness probe

A separate local probe confirmed that Valhalla 3.8.3 applies an embedded
historical profile to both depart-at routes and matrices. This tests engine
behavior only. It is not real traffic data or a production integration.

On September 4, 2026, an isolated Chicago probe kept one 240 m edge at 17.632
seconds at 02:00, then changed it from 17.632 to 86.4 seconds at 08:00. The
time-dependent matrix changed from 17 to 86 seconds. The traffic-free baseline
remained byte-identical. This establishes engine behavior, not a traffic source
or national hosting result.

Keep this probe copy-on-write:

1. Clone the timezone-aware tile directory and point a new config's `tile_dir`
   at the clone.
2. Remove `tile_extract` and `traffic_extract` from that config so the Actor
   cannot load an unchanged archive instead of the cloned files.
3. Use `locate` with `verbose: true` to identify each directed edge. Derive its
   CSV path by replacing the `.gph` suffix from `GraphId.__fspath__()` with
   `.csv`.
4. Create rows of
   `level/tile/id,freeflow_kph,constrained_kph,encoded_profile`, without a
   header. A profile is 2,016 Sunday-first five-minute `float32` buckets,
   compressed to 200 `int16` coefficients and Base64-encoded with pyvalhalla.
5. Run the wheel's packaged `valhalla_add_predicted_traffic` binary against the
   cloned config and CSV root. Verify `predicted: true` with `locate`, then
   compare identical route and matrix requests at quiet and peak times.

A sparse CSV is safe here because every run starts from a traffic-free clone.
Do not treat it as an incremental patch over a populated historical tileset.
See Valhalla's [historical traffic format][historical-traffic], [Python
compression API][predicted-speeds], and [time-dependent matrix rules][matrix].

[historical-traffic]: https://valhalla.github.io/valhalla/concepts/historical-traffic/
[predicted-speeds]: https://valhalla.github.io/valhalla/python/predicted_speeds/
[matrix]: https://valhalla.github.io/valhalla/api/matrix/#time-dependent-matrices

## Deterministic checks

The tests use a fake Actor, clock, and RSS sampler. They need neither
`pyvalhalla` nor road data:

```sh
.venv/bin/ruff check experiments/routing_engines
.venv/bin/python -m pytest experiments/routing_engines
```

The example road data is derived from OpenStreetMap and remains subject to the
Open Database License and OpenStreetMap attribution requirements. Valhalla is
MIT-licensed. See the project-level `THIRD-PARTY-NOTICES.md` and data documentation before
publishing derived results or a service.
