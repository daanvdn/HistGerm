from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_wheel_installs_and_loads_bundled_inventory_outside_checkout(
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-sources",
            "--out-dir",
            str(dist),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("histgerm-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "histgerm/resources/inventory/snapshot.json" in names
        assert "histgerm/resources/inventory/manifest.json" in names
        assert not any(name.startswith("inventory/") for name in names)
        assert not any(name.endswith((".yaml", ".yml")) for name in names)

    environment = tmp_path / "environment"
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--offline",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    script = (
        "import json; "
        "from histgerm.packaging import load_verified_bundled_catalog; "
        "c=load_verified_bundled_catalog(); "
        "print(json.dumps({'release':c.inventory_release,'resources':len(c.resources)}))"
    )
    clean_environment = os.environ.copy()
    clean_environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(python), "-I", "-c", script],
        cwd=outside,
        env=clean_environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "release": "2026.08.11.1",
        "resources": 3,
    }
