from __future__ import annotations

from copy import deepcopy


def status_payload() -> dict[str, object]:
    return {
        "request_id": "request-1",
        "service": {"state": "ready"},
        "cpu": {"state": "available", "policies": [{"policy": 0, "affected_cpus": [0], "driver": "test", "governor": "powersave", "available_governors": ["powersave"], "hardware_min_khz": 1, "hardware_max_khz": 2, "configured_min_khz": 1, "configured_max_khz": 2}]},
        "gpu": {"state": "available", "index": 0, "name": "Test GPU", "driver_version": "test", "power_limit_w": "2", "power_limit_min_w": "1", "power_limit_max_w": "3"},
        "logind": {"state": "available", "can_suspend": "yes", "inhibitors": []},
        "lifecycle": {"state": "active", "blockers": [], "idle_started_at": None, "grace_started_at": None, "next_transition_at": None, "leases": {"active_count": 0, "active_principal_count": 0, "principal_ids": [], "principals_truncated": False}, "last_result": None},
    }


def profile_catalog_payload(*, state: str = "available") -> dict[str, object]:
    return {
        "request_id": "profiles-1",
        "state": state,
        "names": ["balanced", "performance"],
        "reconciliation": {"state": "matched", "name": "balanced"} if state == "available" else {"state": "unavailable"},
    }


def broker_status_payload() -> dict[str, object]:
    value = deepcopy(status_payload())
    value.pop("request_id")
    value.pop("service")
    value["observed_at"] = "2026-01-01T00:00:00Z"
    value["service_state"] = "ready"
    return value


def profile_application_payload() -> dict[str, object]:
    return {
        "request_id": "apply-1",
        "application": {
            "profile": "balanced",
            "outcome": "applied",
            "cpu": {"state": "applied", "policies": [{"policy": 0, "state": "applied"}]},
            "gpu": {"state": "applied", "index": 0},
        },
        "status": broker_status_payload(),
    }


def suspend_receipt_payload() -> dict[str, object]:
    status = deepcopy(status_payload())
    status.pop("request_id")
    return {
        "request_id": "suspend-1",
        "suspend": {"outcome": "accepted", "can_suspend": "yes"},
        "status": status,
    }


def lease_payload(*, request_id: str = "lease-1", lease_id: str = "123e4567-e89b-12d3-a456-426614174000", ttl_seconds: int = 30) -> dict[str, object]:
    return {
        "request_id": request_id,
        "lease": {
            "id": lease_id,
            "expires_at": "2026-09-12T12:00:00Z",
            "ttl_seconds": ttl_seconds,
            "future_field": "ignored",
        },
        "status": {"future_status": "ignored"},
        "future_root_field": True,
    }


def lease_collection_payload(*leases: dict[str, object]) -> dict[str, object]:
    return {"request_id": "leases-1", "leases": list(leases)}
