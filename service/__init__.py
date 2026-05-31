"""Level-2 local service for the BBIN Hydropower Platform.

A continuously-running, read-only control plane plus a background forecast-cycle
scheduler that drives the verified `bbin_platform` modules. Standard-library only
(no external dependencies beyond numpy, which the modeling modules already use),
so it runs anywhere Python 3.11+ is available.

DEMO SCOPE: synthetic data, in-memory state, no message broker / lakehouse /
counterparty transport, and no executed legal instruments. It demonstrates the
verified logic running as a live service.
"""

__version__ = "0.1.0"
