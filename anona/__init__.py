from .client import AnonaClient, AnonaError, RateLimit, RetrieveWithReceipt

# `Anona` is what the TypeScript package calls its client, and the two SDKs
# describing the same API under two different class names is a papercut every
# reader of both pays. The Python name stays canonical — it is what every
# existing import and every published example uses — and this is an alias, not
# a rename, so nothing breaks either way round.
Anona = AnonaClient

__all__ = [
    "Anona",
    "AnonaClient",
    "AnonaError",
    "RateLimit",
    "RetrieveWithReceipt",
]
__version__ = "0.12.1"
