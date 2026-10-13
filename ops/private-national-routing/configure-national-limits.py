#!/usr/bin/env python3
import json
import os
import pathlib
import shutil
import time

data_dir = pathlib.Path(
    os.environ.get("ROUTING_DATA_DIR", pathlib.Path(__file__).resolve().parent / "data")
)
path = data_dir / "valhalla.json"
data = json.loads(path.read_text())
auto = data["service_limits"]["auto"]
auto["max_distance"] = 8_000_000.0
auto["max_matrix_distance"] = 8_000_000.0
thor = data["thor"]
thor["source_to_target_algorithm"] = "select_optimal"
thor.setdefault("costmatrix", {})["allow_second_pass"] = True
thor["costmatrix"]["check_reverse_connection"] = True
backup = path.with_name(
    f"valhalla.json.pre-national-limits.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
)
shutil.copy2(path, backup)
path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
print(f"updated {path}; backup {backup}")
