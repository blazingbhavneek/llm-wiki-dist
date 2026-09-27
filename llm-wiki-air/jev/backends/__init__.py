"""Backend contract: prepare on submitter threads; run and close on engine worker."""

from dataclasses import dataclass


@dataclass(slots=True)
class Prepared:
    request: object
    cost: int
    payload: object


def make_backend(config):
    if config.backend == "torch":
        from .torch import TorchBackend
        return TorchBackend.from_config(config)
    if config.backend == "gguf":
        from .gguf import GGUFBackend
        return GGUFBackend.from_config(config)
    if config.backend == "hosted":
        from .hosted import HostedBackend
        return HostedBackend(config)
    raise ValueError(f"unknown Jev backend: {config.backend}")
