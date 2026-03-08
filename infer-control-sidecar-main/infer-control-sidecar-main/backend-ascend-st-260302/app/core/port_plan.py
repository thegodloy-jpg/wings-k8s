# AUTOGEN_FILE_COMMENT
# -----------------------------------------------------------------------------
# File: core/port_plan.py
# Purpose: Derives deterministic backend/proxy/health port plan for launcher mode.
# Status: Active launcher contract module.
# Responsibilities:
# - Keep behavior stable while improving maintainability via explicit documentation.
# - Clarify how this file participates in launcher/proxy/health sidecar architecture.
# Sidecar Contracts:
# - Maintain fixed sidecar defaults (17000/18000/19000).
# - Respect ENABLE_REASON_PROXY behavior constraints.
# -----------------------------------------------------------------------------
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortPlan:
    enable_proxy: bool
    backend_port: int
    proxy_port: int
    health_port: int


def derive_port_plan(*, port: int, enable_reason_proxy: bool, health_port: int = 19000) -> PortPlan:
    if enable_reason_proxy:
        return PortPlan(
            enable_proxy=True,
            backend_port=17000,
            proxy_port=port or 18000,
            health_port=health_port,
        )
    return PortPlan(
        enable_proxy=False,
        backend_port=port or 18000,
        proxy_port=0,
        health_port=health_port,
    )

