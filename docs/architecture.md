# Architecture

modo has two layers:

- The Python package calculates geographic centers, exact static-road
  objectives, near-optimal vertex sets, travel times, and road-vertex paths.
- The web interface minimizes the longest drive and shows the fixed one-minute
  near-optimal region. It has no venue search or total-time control.

## Interface contract

One Python WSGI process serves the browser assets and JSON API. It loads one
immutable compact road snapshot on first use. For each request it:

1. rejects origins outside the snapshot's supported core, then snaps between
   two and 32 confirmed origins to stored road vertices;
2. deduplicates identical snapped vertices and runs an exact, memory-budgeted
   simultaneous frontier, failing clearly if the group exceeds that budget;
3. returns the complete vertex set within 60 seconds of the optimum;
4. reconstructs one shortest road-vertex path from predecessor labels retained
   by that same bounded search; and
5. returns the snapshot, cost profile, package version, and tolerance with the
   result.

The browser uses one color per origin across its input, pin, route, and travel
time. Pins remain at confirmed coordinates; dotted connectors show road-vertex
snaps. The region is drawn as separate road points so disconnected components
are not joined into invented coverage.

The browser sends address queries directly to Photon and calculates only when
the confirmed origin signature changes. modo receives only confirmed
coordinates and retains no application state between evaluations.
Anonymous evaluation requests share an identifier-free process token bucket so
one client cannot continuously occupy the free service's only worker.

## Engine contract

The NetworkX reference backend and compact sparse backend implement the same
optimization and route contracts. For one graph and origin set they can:

- compute total-time or maximum-time results from reusable shortest-path
  fields;
- return the complete road-vertex region for a nonnegative objective
  tolerance;
- evaluate travel times at any mutually reachable stored vertex; and
- return one shortest road-vertex path per origin to that vertex.

The total-time objective is a library API, not a web interface option.

The simultaneous frontier stops only after every label through the optimum
plus 60 seconds is settled. Its label and heap budgets never truncate or
approximate a result. A request that exceeds them fails instead of starting a
full-graph fallback.

The hosted budget is `ceil(0.025 * distinct_origins * graph_vertices)`, clamped
to 10,000 through 50,000 discovered origin-vertex labels. Heap pushes are
limited to four times that budget. The 10,000 minimum lets a downtown-to-Wrigley
pair finish its complete region with 5,310 labels; the old 5,000 minimum rejected
that ordinary group. The maximum and the algorithm are unchanged.

### Budget measurements

Run `uv run --locked python scripts/benchmark_web_budget.py` on Linux or macOS
with the local Chicago snapshot. It makes no network requests. Each case and
minimum runs three complete WSGI evaluations in a fresh process; regional grids
use public synthetic coordinates snapped onto stored road vertices.

On September 6, 2026, Python 3.12.2/macOS arm64 produced these medians:

| Origins | Old/new budget | Old/new median (ms) | Old/new result |
| --- | --- | --- | --- |
| Downtown/Wrigley pair | 5,000 / 10,000 | 7.30 / 7.96 | Limit / 180 vertices |
| Nearby pair | 5,000 / 10,000 | 1.76 / 1.71 | 149 / 149 vertices |
| Six nearby | 9,512 / 10,000 | 3.06 / 3.44 | 152 / 152 vertices |
| Eight nearby | 12,683 / 12,683 | 3.61 / 3.59 | 116 / 116 vertices |
| 32 nearby | 50,000 / 50,000 | 46.78 / 44.39 | 211 / 211 vertices |
| Eight regional | 12,683 / 12,683 | 17.35 / 17.56 | Limit / limit |
| 32 regional | 50,000 / 50,000 | 78.55 / 77.55 | Limit / limit |
| Opposite corners | 5,000 / 10,000 | 6.49 / 13.40 | Limit / limit |

Peak process RSS, including imports and snapshot loading, stayed below 126 MiB
in every case; the highest with the new minimum was 124.2 MiB. The slowest
individual evaluation took 80.1 ms. These local measurements support the modest
increase within the 512 MiB hosting plan; they do not measure Linux production
latency, concurrent traffic, or Gunicorn master/host overhead. Retain one worker,
the hard maximum, and explicit failures; remeasure before further increases.

Compact snapshots retain vertex coordinates and weighted adjacency, not full
OpenStreetMap edge geometry. Routes therefore show the chosen vertex sequence
but may omit curves between vertices.

## Data and deployment

Road data is a versioned release artifact. Its catalog records the URL,
checksum, bounds, and cost profile. The fetch script verifies the checksum
before installation. CI and the runtime additionally validate directedness,
positive edge costs, structure, and declared graph bounds before reporting
provenance. Future builds emit a checksummed provenance manifest. The initial
static Chicago snapshot is described in the [data notes](../data/README.md).
Its supported core is inset from the graph boundary; the halo reduces boundary
exposure without claiming equivalence to roads outside the named graph.

`render.yaml` and `uv.lock` reproduce the hosted service. No database or
separate routing service is required.

## Geographic expansion

Any lower-48 static fallback should use immutable regional snapshots, each with
an inner supported core and outer routing halo. Build and benchmark each
snapshot offline, then deploy only bundles within measured limits.

Requests must fit one active core. Reject cross-region inputs until prebuilt
partition and boundary routing can preserve the exact-over-the-named-graph
contract. Runtime road downloads are not interim coverage.

An isolated [browser-routing experiment](../experiments/browser-routing/README.md)
tests exact static Worker delivery without changing the production design.

### Exhaustive-search scale limit

Exhaustive NetworkX search is the exact correctness oracle, not a serving
architecture. Measured on 2026-08-25 with an Apple M2, 8 GB RAM, macOS 26.6.1,
Python 3.12.2 and the local Chicago OSM graph, running each size in a fresh
process:

| Scale | Vertices | Edges | Four-search median |
| ---: | ---: | ---: | ---: |
| 0.25 | 4,485 | 12,435 | 0.051 s |
| 0.50 | 17,613 | 48,550 | 0.382 s |
| 0.75 | 39,567 | 106,961 | 0.861 s |
| 1.00 | 63,138 | 168,802 | 1.459 s |

Loading the 63,413-vertex GraphML graph and solving the four-origin minimax case
peaked at 1,449,771,008 bytes resident and took 12.60 seconds end to end. Nested
NetworkX subgraph views were rejected from the harness because their proxy
overhead materially distorted search timings.

This is why a tiled routing engine owns lower-48 coverage: the oracle stays
simple and exact for validation, and the engine places graph-heavy work in
native code. Do not write a custom routing engine. Reconsider native components
only after profiling candidate generation or scoring outside the router. OSRM
MLD remains a useful static comparison because it partitions the graph and can
re-customize edge weights, while its contraction-hierarchy path suits weights
that do not need live updates.

These measurements predate this repository's history, which begins at the
initial public source commit. They were recorded in an earlier
`docs/road-architecture.md`; those original commits are retained in a private
local archive.

## Free-first rollout

Keep the current regional Python service as the deployed baseline. Render's free
web service currently supplies 512 MB RAM and 750 shared instance-hours per
workspace, but sleeps after 15 idle minutes. That is appropriate for measured
regional bundles, not an assumed lower-48 monolith.

The browser Worker experiment remains a traffic-unaware fallback. It moves
immutable road tiles to object storage so the browser owns calculation memory.
Cloudflare R2 currently includes 10 GB-month of Standard storage, 10 million
monthly reads, and free egress; Pages limits individual assets to 25 MiB, so the
shell can live on Pages while graph tiles live in R2. GitHub Pages is suitable
for the shell but caps a published site at 1 GB, so it is not the lower-48 graph
store. These limits were checked on 2026-08-31 and must be checked again before
deployment:

- [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/)
- [Cloudflare Pages limits](https://developers.cloudflare.com/pages/platform/limits/)
- [GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
- [Render free-service limits](https://render.com/docs/free)

Do not create an account or publish data until a lower-48 build measures total
compressed size, file count and maximum tile size, cold and warm tile loads,
browser peak memory, latency, caching, snapping, route reconstruction, and
cancellation. If that gate fails, retain regional services.

Traffic-aware lower-48 coverage should use a tiled routing engine or external
provider rather than enlarge the current Python snapshot. A local Valhalla
3.8.3 Chicago probe loaded an 83.5 MB tile extract at about 184 MB peak process
memory. A separate copy-on-write test changed one route from 17.632 to 86.4
seconds at the synthetic peak, and changed its time-dependent matrix from 17 to
86 seconds. The unchanged baseline remained byte-identical.

That proves the engine path, not the data path. Valhalla needs a lawful national
historical or live traffic source. A bounded [TomTom capability
probe](../experiments/tomtom/README.md) on September 6, 2026 confirmed that modo's
key can calculate a traffic-aware route and reachable-range polygon. That
resolves access for those two endpoints, but it does not establish an exact
road-vertex region or a reusable traffic dataset. Mapbox explicitly excludes
its traffic profile from isochrones. Any external provider still needs verified
terms, privacy, failure behavior, and cost; any polygon approximation needs a
separate, explicit product decision.

- [Valhalla traffic experiment](../experiments/routing_engines/README.md)
- [TomTom reachable range](https://docs.tomtom.com/routing-api/documentation/tomtom-maps/v1/calculate-reachable-range)
- [TomTom pricing](https://docs.tomtom.com/pricing)
- [Mapbox traffic-profile limits](https://docs.mapbox.com/help/glossary/routing-profile/)

## fairway boundary

fairway ranks a finite golf-course catalog; modo searches mutually reachable
road vertices. fairway may reuse package-level routing code, but the web
applications remain independent and do not call one another.
