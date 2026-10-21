"""One client module per external data source.

``CLIENTS`` is the single dispatch point from a source name to its client
factory; the CLI resolves ``--source`` here and nowhere else.
"""

from collections.abc import Callable, Mapping
from typing import Final

from wattfrosch_fetch.clients import energy_charts, open_meteo, smard
from wattfrosch_fetch.http import BaseClient, FetchConfigError

CLIENTS: Final[Mapping[str, Callable[[], BaseClient]]] = {
    energy_charts.SOURCE: energy_charts.EnergyChartsClient,
    open_meteo.SOURCE: open_meteo.OpenMeteoClient,
    smard.SOURCE: smard.SmardClient,
}


def create_client(source: str) -> BaseClient:
    """Instantiate the client for ``source``; unknown sources raise exit-code 1."""
    try:
        factory = CLIENTS[source]
    except KeyError:
        raise FetchConfigError(
            f"unknown source {source!r}; known sources: {sorted(CLIENTS)}",
        ) from None
    return factory()
