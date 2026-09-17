# modo

`modo` is a map and Python library for multi-origin road meeting regions. The
[hosted interface](https://modo-m4as.onrender.com) minimizes the longest drive
from two or more origins and shows every stored road vertex within 60 seconds
of the optimum.

The current interface uses a static Chicago-area snapshot without traffic. Its
points are neither venue recommendations nor assurances of a safe stopping
place. See the [model](docs/model.md), [architecture](docs/architecture.md), and
[service policy](SERVICE.md) for calculation, data, and privacy details.

## Run

Python 3.12 or newer is required.

```sh
python -m pip install uv==0.12.15
uv sync --extra app --extra test --locked
uv run --locked python scripts/fetch_snapshot.py
uv run --locked gunicorn modo.web:application
```

Open `http://127.0.0.1:8000`. Address suggestions come from the public Photon
service. Coordinates can also be confirmed as `latitude, longitude`.

`render.yaml` reproduces the hosted service from `uv.lock` and the checksummed
snapshot catalog.

## Python library

The app is minimax-only. The package also supports total-time road results and
general geographic centers.

```python
from modo import CompactRoadGraph

roads = CompactRoadGraph.load("roads.npz")
analysis = roads.analyze_coordinates(origin_coordinates)
result = analysis.optimize("maximum", tolerance_seconds=60)
routes = analysis.routes(result.vertex)
region_coordinates = roads.coordinates(result.region)
```

`result.region` is the complete qualifying vertex set;
`region_excess_seconds` gives each vertex's distance above the optimum. The
NetworkX backend exposes the same contracts, and compact analysis accepts
`retain_distances=False` for lower-memory scoring. A maximum-only caller can
also pass `objective="maximum"` to omit total-time scoring arrays. Exact
semantics are in the [mathematical model](docs/model.md).

## Checks

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python -m pytest tests experiments
uv run --locked python -m build
uv run --locked python scripts/validate_snapshot.py
```

Tests use synthetic fixtures and require no geographic data or external services.
The browser experiment also requires Node.js 24 or newer.

## Icon

[docs/icon.png](docs/icon.png) is the approved folded-map source. Keep that
image when iterating; regenerate the two small browser assets after replacing
it. On macOS, from the repository root:

```sh
sips -z 180 180 docs/icon.png --out src/modo/static/icon.png
sips -z 32 32 docs/icon.png --out src/modo/static/favicon.png
```

The 180px image serves both the header and saved home-screen shortcut. The
32px image is the favicon. Copy the full-resolution source to snowball's
website project image when updating it there.

## License

modo is a snowball project licensed under the [MIT License](LICENSE).
The road snapshot is separately licensed under the Open Database License, and
the local Leaflet assets remain BSD-2-Clause. See
[data notes](data/README.md), [third-party notices](THIRD-PARTY-NOTICES.md),
[contribution terms](CONTRIBUTING.md), and the
[Leaflet license](src/modo/static/LEAFLET-LICENSE.txt).

[Operations](https://snowball-projects.github.io/operations/#modo)
