"""Region validity clamping shared by all bidding-zone clients.

The contract defines regions as SCD Type 2 with half-open
``[valid_from, valid_to)`` windows. Some APIs ignore these windows
(Energy-Charts serves pre-2018 data under ``de-lu``), so clients clamp the
requested window to the validity range and only emit ``ts_utc`` where the
region is valid. A window fully outside the validity range yields an empty
result without a request.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from wattfrosch_fetch.contracts import REGIONS


def clamp_to_validity(
    region: str,
    start: datetime,
    end: datetime,
) -> tuple[datetime, datetime]:
    """Intersect ``[start, end)`` with the region's validity window."""
    entry = REGIONS[region]
    valid_from = datetime.combine(entry.valid_from, datetime.min.time(), tzinfo=UTC)
    clamped_start = max(start, valid_from)
    clamped_end = end
    if entry.valid_to is not None:
        valid_to = datetime.combine(entry.valid_to, datetime.min.time(), tzinfo=UTC)
        clamped_end = min(end, valid_to)
    return clamped_start, clamped_end


__all__: Final[tuple[str, ...]] = ("clamp_to_validity",)
