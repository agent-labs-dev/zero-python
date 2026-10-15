from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_zero_version_is_one_semver() -> None:
    version = (ROOT / "ZERO_VERSION").read_text().strip()
    major, minor, patch = version.split(".")
    assert all(part.isdigit() for part in (major, minor, patch))


def test_package_imports() -> None:
    import zero_python

    assert zero_python.__version__.count(".") == 2
