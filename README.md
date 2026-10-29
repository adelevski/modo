# modo

`modo` is a map and Python library for multi-origin road meeting regions. The
[hosted interface](https://modo-m4as.onrender.com) minimizes the longest drive
from two or more origins and shows every stored road vertex within 60 seconds
of the optimum.

The current interface uses a static Chicago-area snapshot without traffic. Its
points are neither venue recommendations nor assurances of a safe stopping
place. See the [model](docs/model.md) and [service policy](SERVICE.md) for
calculation, data, and privacy details.

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

The app is minimax-only. The package also supports total-time road results.

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

## License

modo is licensed under the [MIT License](LICENSE).
The road snapshot is separately licensed under the Open Database License, and
the local Leaflet assets remain BSD-2-Clause. See
[data notes](data/README.md), [third-party notices](THIRD-PARTY-NOTICES.md), and the
[Leaflet license](src/modo/static/LEAFLET-LICENSE.txt).

