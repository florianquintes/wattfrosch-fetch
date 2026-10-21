"""Contract version check: fail fast at import on incompatible contracts.

The contract version is checked at import time: major.minor must match the
supported version, patch releases are accepted (1b). A mismatch fails fast
with :class:`ContractVersionError` instead of producing bad data later.
"""

from __future__ import annotations

import re
from typing import Final

from wattfrosch_contracts import CONTRACT_VERSION

__all__ = [
    "CONTRACT_VERSION_SUPPORTED",
    "ContractVersionError",
    "assert_contract_version",
]

CONTRACT_VERSION_SUPPORTED: Final[str] = "0.1"
_CONTRACT_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+$")


class ContractVersionError(RuntimeError):
    """The installed wattfrosch-contracts version is incompatible."""


def assert_contract_version(version: str) -> None:
    """Raise :class:`ContractVersionError` unless major.minor matches.

    The version must be a full ``MAJOR.MINOR.PATCH`` string (no pre-release
    suffixes); only the major.minor part is compared, patch releases are
    accepted.

    Args:
        version: The ``CONTRACT_VERSION`` string of the installed contracts
            package.

    Raises:
        ContractVersionError: If the version is malformed or its major.minor
            part does not equal ``CONTRACT_VERSION_SUPPORTED``.
    """
    supported = tuple(int(part) for part in CONTRACT_VERSION_SUPPORTED.split("."))
    if (
        _CONTRACT_VERSION_RE.match(version) is None
        or tuple(int(part) for part in version.split(".")[:2]) != supported
    ):
        msg = (
            f"Unsupported wattfrosch-contracts version {version!r}: "
            f"expected major.minor {CONTRACT_VERSION_SUPPORTED}.x"
        )
        raise ContractVersionError(msg)


assert_contract_version(CONTRACT_VERSION)
