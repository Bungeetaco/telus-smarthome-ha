"""Shared entity base + capability helpers for TELUS SmartHome+.

The Platform device-list JSON shape is not 100% confirmed from the binary, so
capability lookup is written defensively: it searches a few likely shapes
(a ``capabilities`` list of objects, or a dict keyed by capability id). Once a
real device-list response is captured, tighten `iter_capabilities()`.
"""
from __future__ import annotations

from typing import Any, Iterable

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import TelusCoordinator


def device_id_of(dev: dict[str, Any]) -> str:
    return str(dev.get("deviceId") or dev.get("id") or dev.get("assetId") or "")


def device_type_of(dev: dict[str, Any]) -> str:
    meta = dev.get("smartHomeDeviceMetadata") or {}
    return str(
        dev.get("type")
        or dev.get("deviceType")
        or dev.get("category")
        or meta.get("type")
        or ""
    ).lower()


def metadata_of(dev: dict[str, Any]) -> dict[str, Any]:
    return dev.get("smartHomeDeviceMetadata") or {}


def resources_of(dev: dict[str, Any]) -> list[dict[str, Any]]:
    res = metadata_of(dev).get("resources")
    return [r for r in res if isinstance(r, dict)] if isinstance(res, list) else []


def get_resource(dev: dict[str, Any], href: str) -> dict[str, Any] | None:
    """Return the OCF resource with this href (e.g. '/siren', '/pirSensor')."""
    for r in resources_of(dev):
        if r.get("href") == href:
            return r
    return None


def is_online(dev: dict[str, Any]) -> bool:
    meta = metadata_of(dev)
    status = str(meta.get("connectivityStatus") or "").lower()
    return status == "connected" or bool(dev.get("status"))


def device_name_of(dev: dict[str, Any]) -> str:
    return (
        dev.get("name")
        or dev.get("label")
        or dev.get("displayName")
        or f"TELUS device {device_id_of(dev)}"
    )


def iter_capabilities(dev: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield capability objects regardless of container shape."""
    caps = dev.get("capabilities")
    if isinstance(caps, list):
        yield from (c for c in caps if isinstance(c, dict))
    elif isinstance(caps, dict):
        for key, val in caps.items():
            if isinstance(val, dict):
                yield {"identifier": key, **val}
            else:
                yield {"identifier": key, "value": val}


def capability_ids(dev: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for cap in iter_capabilities(dev):
        ident = cap.get("identifier") or cap.get("type") or cap.get("name")
        if ident:
            # normalize "BinarySwitchCapability" / "binarySwitch" -> "binaryswitch"
            out.add(str(ident).replace("Capability", "").lower())
    return out


def get_capability(dev: dict[str, Any], *names: str) -> dict[str, Any] | None:
    wanted = {n.replace("Capability", "").lower() for n in names}
    for cap in iter_capabilities(dev):
        ident = cap.get("identifier") or cap.get("type") or cap.get("name") or ""
        if str(ident).replace("Capability", "").lower() in wanted:
            return cap
    return None


class TelusEntity(CoordinatorEntity[TelusCoordinator]):
    """Base entity bound to one device id in the coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TelusCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id

    @property
    def device(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._device_id, {})

    @property
    def available(self) -> bool:
        return super().available and self._device_id in self.coordinator.data

    @property
    def device_info(self) -> DeviceInfo:
        dev = self.device
        meta = metadata_of(dev)
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=device_name_of(dev),
            manufacturer=meta.get("manufacturer") or MANUFACTURER,
            model=meta.get("modelNumber") or dev.get("type") or dev.get("model"),
            sw_version=meta.get("modelVersion") or dev.get("firmwareVersion"),
            serial_number=meta.get("serialNumber"),
        )

    async def _send(self, capability: str, operation: str) -> None:
        dev = self.device
        await self.coordinator.api.async_send_command(
            self._device_id,
            capability,
            operation,
            connector_id=dev.get("connectorId") or metadata_of(dev).get("connectorId"),
            provider=dev.get("provider"),
        )
        await self.coordinator.async_request_refresh()
