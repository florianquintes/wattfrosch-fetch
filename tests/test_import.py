"""Import smoke test: every module in wattfrosch_fetch must be importable."""

import importlib
import pkgutil

import wattfrosch_fetch


def test_package_imports() -> None:
    assert wattfrosch_fetch.__doc__ or wattfrosch_fetch is not None


def test_all_modules_import() -> None:
    submodules = [
        info.name
        for info in pkgutil.walk_packages(
            wattfrosch_fetch.__path__, prefix="wattfrosch_fetch."
        )
    ]
    for name in submodules:
        importlib.import_module(name)
