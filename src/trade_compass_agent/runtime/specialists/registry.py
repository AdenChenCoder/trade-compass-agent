from __future__ import annotations

from trade_compass_agent.runtime.specialists.assets import SpecialistProfile, load_specialist_profiles


def get_specialist(name: str) -> SpecialistProfile | None:
    return load_specialist_profiles().get(name)


def list_specialists() -> list[SpecialistProfile]:
    return list(load_specialist_profiles().values())
