from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_zero_version_is_one_semver() -> None:
    version = (ROOT / "ZERO_VERSION").read_text().strip()
    major, minor, patch = version.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))


def test_package_imports() -> None:
    import zero_python

    assert zero_python.__version__.count(".") == 2


def test_harnesses_run_the_pinned_zero() -> None:
    import json

    version = (ROOT / "ZERO_VERSION").read_text().strip()
    for project in ("harness", "e2e"):
        package = json.loads((ROOT / project / "package.json").read_text())
        lock = json.loads((ROOT / project / "package-lock.json").read_text())
        assert package["dependencies"]["@rocicorp/zero"] == version
        assert lock["packages"]["node_modules/@rocicorp/zero"]["version"] == version


def test_e2e_runs_the_pinned_zero_cache() -> None:
    version = (ROOT / "ZERO_VERSION").read_text().strip()
    assert f"image: rocicorp/zero:{version}" in (ROOT / "e2e" / "docker-compose.yml").read_text()
