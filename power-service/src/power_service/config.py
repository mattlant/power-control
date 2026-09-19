from __future__ import annotations
import os, stat, tomllib
from ipaddress import ip_address
from pathlib import Path
from decimal import Decimal
from .models import *


def validate_file_permissions(path: Path, max_mode: int = 0o640) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o007 or mode & ~max_mode: raise ValueError("insecure configuration permissions")


def _read(path):
    validate_file_permissions(path)
    data = tomllib.loads(path.read_text())
    if _has_command(data): raise ValueError("command configuration is prohibited")
    return data


def _has_command(value):
    if isinstance(value, dict): return "command" in value or any(_has_command(x) for x in value.values())
    if isinstance(value, list): return any(_has_command(x) for x in value)
    return False


def _only(d, allowed):
    if set(d) != set(allowed): raise ValueError("unknown or missing configuration key")


def load_api_config(path: Path) -> ApiConfig:
    d = _read(path)
    _only(d, {"listen_host", "listen_port", "broker_socket", "tls_cert", "tls_key", "request_timeout_seconds",
              "credentials"})
    credentials = []
    for c in d["credentials"]:
        _only(c, {"id", "salt_hex", "scrypt_hash_hex", "permissions"})
        credentials.append(CredentialRecord(c["id"], bytes.fromhex(c["salt_hex"]), bytes.fromhex(c["scrypt_hash_hex"]),
                                            frozenset(Permission(x) for x in c["permissions"])))
    return ApiConfig(ip_address(d["listen_host"]), d["listen_port"], Path(d["broker_socket"]), Path(d["tls_cert"]),
                     Path(d["tls_key"]), d["request_timeout_seconds"], tuple(credentials))


def load_broker_config(path: Path) -> BrokerConfig:
    d = _read(path)
    _only(d, {"socket_path", "api_uid", "gpu_index", "query_timeout_seconds", "operation_timeout_seconds", "profiles",
              "automatic_suspend"} if "automatic_suspend" in d else {"socket_path", "api_uid", "gpu_index",
                                                                     "query_timeout_seconds",
                                                                     "operation_timeout_seconds", "profiles"})
    profiles = []
    if not isinstance(d["profiles"], list): raise ValueError("profiles must be an array")
    for p in d["profiles"]:
        _only(p, {"name", "cpu", "gpu"})
        cpu = p["cpu"]
        gpu = p["gpu"]
        _only(cpu,
              {"policy_ids", "governor", "min_khz", "max_khz", "epp"} if "epp" in cpu else {"policy_ids", "governor",
                                                                                            "min_khz", "max_khz"})
        _only(gpu, {"power_limit_w"})
        profiles.append(ProfileDefinition(p["name"],
                                          CpuProfileAction(tuple(cpu["policy_ids"]), cpu["governor"], cpu["min_khz"],
                                                           cpu["max_khz"], cpu.get("epp")),
                                          GpuProfileAction(Decimal(str(gpu["power_limit_w"])))))
    auto = d.get("automatic_suspend")
    if auto is None:
        return BrokerConfig(Path(d["socket_path"]), d["api_uid"], d["gpu_index"], d["query_timeout_seconds"],
                            d["operation_timeout_seconds"], tuple(profiles))
    required_auto = {"enabled", "max_lease_ttl_seconds", "stable_idle_seconds", "grace_seconds",
                     "evaluation_interval_seconds", "max_status_principals", "local_activity_probes"}
    allowed_auto = required_auto | {"interactive_sessions_enabled", "interactive_activity_timeout_seconds"}
    if not isinstance(auto, dict) or not required_auto.issubset(auto):
        raise ValueError("missing automatic suspend configuration key")
    if set(auto) - allowed_auto:
        raise ValueError("unknown automatic suspend configuration key")
    if not isinstance(auto["local_activity_probes"], list):
        raise ValueError("local_activity_probes must be an array")
    probes = []
    for probe in auto["local_activity_probes"]:
        if not isinstance(probe, dict) or "kind" not in probe:
            raise ValueError("invalid local activity probe")
        if probe["kind"] == "process_name":
            _only(probe, {"kind", "name"})
            probes.append(LocalActivityProbeConfig("process_name", probe["name"]))
        elif probe["kind"] == "tcp_listener":
            _only(probe, {"kind", "port"})
            probes.append(LocalActivityProbeConfig("tcp_listener", probe["port"]))
        else:
            raise ValueError("invalid local activity probe kind")
    if len({(p.kind, p.value) for p in probes}) != len(probes): raise ValueError("duplicate local activity probe")
    interactive_enabled = auto.get("interactive_sessions_enabled", False)
    if not isinstance(interactive_enabled, bool):
        raise ValueError("interactive_sessions_enabled must be boolean")
    interactive_activity_timeout = auto.get("interactive_activity_timeout_seconds", 60)
    automatic = AutomaticSuspendConfig(auto["enabled"], auto["max_lease_ttl_seconds"], auto["stable_idle_seconds"],
                                       auto["grace_seconds"], auto["evaluation_interval_seconds"],
                                       auto["max_status_principals"], tuple(probes), interactive_enabled,
                                       interactive_activity_timeout)
    return BrokerConfig(Path(d["socket_path"]), d["api_uid"], d["gpu_index"], d["query_timeout_seconds"],
                        d["operation_timeout_seconds"], tuple(profiles), automatic)
