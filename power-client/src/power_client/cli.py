from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import json
import math
import os
from pathlib import Path
import sys
import tomllib
from typing import TextIO
from ipaddress import IPv4Address
from urllib.parse import urlsplit

from . import (
    ConfigurationError,
    CredentialFileReference,
    HttpReadinessTarget,
    LeaseId,
    LeaseCollection,
    StatusLeaseDetails,
    PowerClientError,
    ProfileApplication,
    ProfileCatalog,
    ProfileName,
    ServiceStatus,
    ServiceConnectionConfig,
    SuspendReceipt,
    TrustConfig,
    compose_client,
    compose_wake_orchestrator,
    WakeRequest,
    WakeTarget,
    ServiceWaitPolicy,
    ReadinessWaitPolicy,
    TcpReadinessTarget,
    WakeResult,
)


@dataclass(frozen=True)
class CliConfiguration:
    connection: ServiceConnectionConfig
    credential_file: CredentialFileReference
    wake_request: WakeRequest | None = None


_AD_HOC_READINESS_POLICY = ReadinessWaitPolicy(120.0, 5.0, 2.0)


def resolve_config_path(explicit_path: str | None, environment: Mapping[str, str]) -> Path:
    if explicit_path is not None:
        selected = explicit_path
    elif "POWERCTL_CONFIG" in environment:
        selected = environment["POWERCTL_CONFIG"]
    else:
        xdg_home = environment.get("XDG_CONFIG_HOME", "")
        if xdg_home:
            xdg_path = Path(xdg_home)
            if not xdg_path.is_absolute():
                raise ConfigurationError("cli", "config", "is invalid")
            selected = str(xdg_path / "power-client" / "config.toml")
        else:
            selected = str(Path.home() / ".config" / "power-client" / "config.toml")
    if not selected:
        raise ConfigurationError("cli", "config", "path must not be empty")
    path = Path(selected)
    return path if path.is_absolute() else Path.cwd() / path


def load_cli_configuration(path: Path) -> CliConfiguration:
    try:
        if not path.is_absolute():
            raise ConfigurationError("cli", "config", "path must be absolute")
        if not path.exists():
            raise ConfigurationError("cli", "config", "file does not exist")
        if not path.is_file():
            raise ConfigurationError("cli", "config", "path is not a regular file")
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except ConfigurationError:
        raise
    except tomllib.TOMLDecodeError:
        raise ConfigurationError("cli", "config", "contains malformed TOML") from None
    except (OSError, UnicodeDecodeError):
        raise ConfigurationError("cli", "config", "cannot be read") from None

    if not isinstance(data, dict):
        raise ConfigurationError("cli", "config", "root must be a TOML table")
    unknown_root_keys = sorted(set(data) - {"service", "wake"})
    if unknown_root_keys:
        raise ConfigurationError("cli", "config", f"unknown root key(s): {', '.join(unknown_root_keys)}")
    if "service" not in data:
        raise ConfigurationError("cli", "config", "missing [service] table")
    if not isinstance(data["service"], dict):
        raise ConfigurationError("cli", "service", "must be a TOML table")
    service = data["service"]
    allowed = {"endpoint", "request_timeout", "credential_file", "additional_ca_bundle_path"}
    unknown_service_keys = sorted(set(service) - allowed)
    if unknown_service_keys:
        raise ConfigurationError("cli", "service", f"unknown key(s): {', '.join(unknown_service_keys)}")
    required = ("endpoint", "request_timeout", "credential_file")
    for key in required:
        if key not in service:
            raise ConfigurationError("cli", f"service.{key}", "is required")
    if not isinstance(service["endpoint"], str):
        raise ConfigurationError("cli", "service.endpoint", "must be a string")
    if type(service["request_timeout"]) not in (int, float):
        raise ConfigurationError("cli", "service.request_timeout", "must be a number")
    if not isinstance(service["credential_file"], str):
        raise ConfigurationError("cli", "service.credential_file", "must be a string")
    if "additional_ca_bundle_path" in service and not isinstance(service["additional_ca_bundle_path"], str):
        raise ConfigurationError("cli", "service.additional_ca_bundle_path", "must be a string")
    try:
        trust_path = service.get("additional_ca_bundle_path")
        trust = TrustConfig(Path(trust_path) if trust_path is not None else None)
        connection = ServiceConnectionConfig(service["endpoint"], service["request_timeout"], trust)
        credential_file = CredentialFileReference(Path(service["credential_file"]))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ConfigurationError("cli", "config", "contains an invalid service value") from None
    wake_request = None
    if "wake" in data:
        if not isinstance(data["wake"], dict):
            raise ConfigurationError("cli", "wake", "must be a TOML table")
        wake = data["wake"]
        allowed_wake = {
            "mac_address", "broadcast_address", "udp_port", "service_wait_timeout", "service_wait_poll_interval",
            "readiness_wait_timeout", "readiness_attempt_timeout", "readiness_wait_poll_interval", "probe",
        }
        unknown_wake_keys = sorted(set(wake) - allowed_wake)
        if unknown_wake_keys:
            raise ConfigurationError("cli", "wake", f"unknown key(s): {', '.join(unknown_wake_keys)}")
        required_wake = ("mac_address", "broadcast_address", "udp_port", "service_wait_timeout", "service_wait_poll_interval")
        for key in required_wake:
            if key not in wake:
                raise ConfigurationError("cli", f"wake.{key}", "is required")
        if not isinstance(wake["mac_address"], str) or not isinstance(wake["broadcast_address"], str):
            raise ConfigurationError("cli", "wake", "addresses must be strings")
        if type(wake["udp_port"]) is not int:
            raise ConfigurationError("cli", "wake.udp_port", "must be an integer")
        for key in ("service_wait_timeout", "service_wait_poll_interval"):
            if type(wake[key]) not in (int, float):
                raise ConfigurationError("cli", f"wake.{key}", "must be a number")
        probe_tables = wake.get("probe", [])
        if not isinstance(probe_tables, list) or any(not isinstance(item, dict) for item in probe_tables):
            raise ConfigurationError("cli", "wake.probe", "must be an array of tables")
        readiness_keys = ("readiness_wait_timeout", "readiness_attempt_timeout", "readiness_wait_poll_interval")
        present_readiness = [key for key in readiness_keys if key in wake]
        if probe_tables and len(present_readiness) != len(readiness_keys):
            raise ConfigurationError("cli", "wake.readiness", "all readiness timing fields are required when probes are configured")
        if not probe_tables and present_readiness:
            raise ConfigurationError("cli", "wake.readiness", "requires at least one probe")
        definitions = []
        identities: set[str] = set()
        for index, table in enumerate(probe_tables):
            probe_name = f"wake.probe[{index}]"
            probe_type = table.get("type")
            identity = table.get("identity")
            if not isinstance(probe_type, str) or not isinstance(identity, str):
                raise ConfigurationError("cli", probe_name, "requires string type and identity")
            if identity in identities:
                raise ConfigurationError("cli", f"{probe_name}.identity", "must be unique")
            identities.add(identity)
            if probe_type == "tcp":
                if set(table) != {"identity", "type", "host", "port"} or not isinstance(table.get("host"), str) or type(table.get("port")) is not int:
                    raise ConfigurationError("cli", probe_name, "requires exactly identity, type, host, and port")
                definitions.append(TcpReadinessTarget(identity, table["host"], table["port"]))
            elif probe_type == "http":
                if set(table) != {"identity", "type", "url", "expected_statuses"} or not isinstance(table.get("url"), str) or not isinstance(table.get("expected_statuses"), list):
                    raise ConfigurationError("cli", probe_name, "requires exactly identity, type, url, and expected_statuses")
                definitions.append(HttpReadinessTarget(identity, table["url"], tuple(table["expected_statuses"])))
            else:
                raise ConfigurationError("cli", f"{probe_name}.type", "must be tcp or http")
        try:
            readiness_policy = ReadinessWaitPolicy(*(wake[key] for key in readiness_keys)) if probe_tables else None
            wake_request = WakeRequest(
                WakeTarget(wake["mac_address"], IPv4Address(wake["broadcast_address"]), wake["udp_port"]),
                ServiceWaitPolicy(wake["service_wait_timeout"], wake["service_wait_poll_interval"]),
                tuple(definitions),
                readiness_policy,
            )
        except ConfigurationError:
            raise
        except ValueError as error:
            raise ConfigurationError("cli", "wake.broadcast_address", "must be an IPv4 address") from error
    return CliConfiguration(connection, credential_file, wake_request)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="powerctl")
    parser.add_argument("--config", dest="config", default=None)
    parser.add_argument("--output", choices=("text", "json"), default="text")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    profile = subparsers.add_parser("profile")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    profile_commands.add_parser("list")
    apply = profile_commands.add_parser("apply")
    apply.add_argument("name")
    subparsers.add_parser("suspend")
    wake = subparsers.add_parser("wake")
    wake.add_argument("readiness_target", nargs="?", help="optional tcp://host:port or http[s]://host/path target")
    wake.add_argument("expected_http_status", nargs="?", help="optional HTTP status, default 200")
    lease = subparsers.add_parser("lease")
    lease_commands = lease.add_subparsers(dest="lease_command", required=True)
    acquire = lease_commands.add_parser("acquire")
    acquire.add_argument("ttl_seconds", type=int)
    lease_commands.add_parser("list")
    renew = lease_commands.add_parser("renew")
    renew.add_argument("lease_id")
    renew.add_argument("ttl_seconds", type=int)
    release = lease_commands.add_parser("release")
    release.add_argument("lease_id")
    return parser


def _ad_hoc_wake_request(configuration: CliConfiguration, parsed: argparse.Namespace) -> WakeRequest:
    configured = getattr(configuration, "wake_request", None)
    if configured is None:
        raise ConfigurationError("cli", "wake", "requires a [wake] configuration table")
    target_text = getattr(parsed, "readiness_target", None)
    status_text = getattr(parsed, "expected_http_status", None)
    if target_text is None:
        if status_text is not None:
            raise ConfigurationError("cli", "wake.expected_http_status", "requires a readiness target")
        return configured

    try:
        parts = urlsplit(target_text)
        scheme = parts.scheme.lower()
        if scheme == "tcp":
            if status_text is not None:
                raise ConfigurationError("cli", "wake.expected_http_status", "is invalid for a TCP readiness target")
            if (
                not parts.netloc
                or parts.path
                or parts.query
                or parts.fragment
                or parts.username is not None
                or parts.password is not None
            ):
                raise ConfigurationError("cli", "wake.readiness_target", "must be tcp://host:port")
            host = parts.hostname
            port = parts.port
            if host is None or port is None:
                raise ConfigurationError("cli", "wake.readiness_target", "must be tcp://host:port")
            probe = TcpReadinessTarget("adhoc-tcp", host, port)
        elif scheme in {"http", "https"}:
            if status_text is None:
                status = 200
            else:
                try:
                    status = int(status_text)
                except (TypeError, ValueError) as error:
                    raise ConfigurationError("cli", "wake.expected_http_status", "must be an integer") from error
            probe = HttpReadinessTarget("adhoc-http", target_text, (status,))
        else:
            raise ConfigurationError("cli", "wake.readiness_target", "must use tcp, http, or https")
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as error:
        raise ConfigurationError("cli", "wake.readiness_target", "is malformed") from error

    policy = configured.readiness_wait_policy or _AD_HOC_READINESS_POLICY
    return WakeRequest(configured.target, configured.service_wait_policy, (probe,), policy)


async def run_command(parsed: argparse.Namespace, configuration: CliConfiguration, *, client_factory=compose_client, orchestrator_factory=compose_wake_orchestrator) -> object:
    if parsed.command == "wake":
        request = _ad_hoc_wake_request(configuration, parsed)
        orchestrator = orchestrator_factory(configuration.connection, configuration.credential_file)
        async with orchestrator:
            return await orchestrator.wake(request)
    if parsed.command == "status":
        operation = "get_status" if parsed.output == "json" else "get_status_with_leases"
        arguments: tuple[object, ...] = ()
    elif parsed.command == "profile" and parsed.profile_command == "list":
        operation = "list_profiles"
        arguments = ()
    elif parsed.command == "profile" and parsed.profile_command == "apply":
        operation = "apply_profile"
        arguments = (ProfileName(parsed.name),)
    elif parsed.command == "suspend":
        operation = "request_suspend"
        arguments = ()
    elif parsed.command == "lease" and parsed.lease_command == "acquire":
        operation = "acquire_lease"
        arguments = (parsed.ttl_seconds,)
    elif parsed.command == "lease" and parsed.lease_command == "list":
        operation = "list_leases"
        arguments = ()
    elif parsed.command == "lease" and parsed.lease_command == "renew":
        operation = "renew_lease"
        arguments = (LeaseId(parsed.lease_id), parsed.ttl_seconds)
    elif parsed.command == "lease" and parsed.lease_command == "release":
        operation = "release_lease"
        arguments = (LeaseId(parsed.lease_id),)
    else:
        raise ConfigurationError("cli", "command", "is invalid")
    client = client_factory(configuration.connection, configuration.credential_file)
    async with client:
        result = await getattr(client, operation)(*arguments)
        if operation == "release_lease":
            return {"lease_id": parsed.lease_id, "released": True}
        return result


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        timestamp = value.astimezone(timezone.utc)
        return timestamp.isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def render_success(value: object, stdout: TextIO) -> None:
    stdout.write(json.dumps(_json_value(value), sort_keys=True, separators=(",", ":")) + "\n")


def _format_duration(seconds: float) -> str:
    remaining = max(0, math.ceil(seconds))
    if remaining < 60:
        return f"{remaining}s"
    minutes, seconds = divmod(remaining, 60)
    if minutes < 60:
        return f"{minutes}m" if seconds == 0 else f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_power(value: Decimal) -> str:
    formatted = format(value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def _format_cpu_range(policies: tuple[object, ...], minimum_field: str, maximum_field: str) -> str:
    ranges = {
        (getattr(policy, minimum_field), getattr(policy, maximum_field))
        for policy in policies
    }
    if len(ranges) != 1:
        return "mixed"
    minimum, maximum = next(iter(ranges))
    return f"{minimum / 1000:g}–{maximum / 1000:g} MHz"


def _render_status(status: ServiceStatus, stdout: TextIO, *, now: datetime, leases: LeaseCollection | None = None) -> None:
    stdout.write(f"Service:    {status.service_state.value}\n\n")
    if status.profiles is None:
        stdout.write("Profile:    unavailable\n\n")
    else:
        profile = status.profiles.reconciliation
        profile_name = profile.name or "none"
        stdout.write(f"Profile:    {profile_name} ({profile.state.value})\n\n")

    stdout.write(f"CPU:        {status.cpu.state.value}\n")
    if status.cpu.value is not None:
        policies = status.cpu.value.policies
        stdout.write(f"            {len(policies)} policies\n")
        governors = {policy.governor for policy in policies}
        governor = next(iter(governors)) if len(governors) == 1 else "mixed"
        stdout.write(f"            governor: {governor}\n")
        current_range = _format_cpu_range(policies, "configured_min_khz", "configured_max_khz")
        capable_range = _format_cpu_range(policies, "hardware_min_khz", "hardware_max_khz")
        stdout.write(f"            current range: {current_range}\n")
        stdout.write(f"            capable range: {capable_range}\n")
        epps = {policy.epp for policy in policies if policy.epp}
        if len(epps) == 1:
            stdout.write(f"            EPP: {next(iter(epps))}\n")
        elif len(epps) > 1:
            stdout.write("            EPP: mixed\n")
    stdout.write("\n")

    stdout.write(f"GPU:        {status.gpu.state.value}\n")
    if status.gpu.value is not None:
        gpu = status.gpu.value
        stdout.write(f"            {gpu.name} ({gpu.driver_version})\n")
        stdout.write(
            f"            power limit: {_format_power(gpu.power_limit_w)} W "
            f"({_format_power(gpu.power_limit_min_w)}–{_format_power(gpu.power_limit_max_w)} W)\n"
        )
    stdout.write("\n")

    stdout.write(f"Suspend:    {status.logind.state.value}\n")
    if status.logind.value is not None:
        logind = status.logind.value
        stdout.write(f"            can suspend: {logind.can_suspend.value}\n")
        stdout.write(f"            inhibitors: {len(logind.inhibitors)}\n")
    stdout.write("\n")
    _render_lifecycle(status, stdout, now=now, leases=leases)


def _render_lifecycle(status: ServiceStatus, stdout: TextIO, *, now: datetime, leases: LeaseCollection | None = None) -> None:
    lifecycle = status.lifecycle
    blockers = tuple(lifecycle.blockers)
    if "lease" in blockers:
        stdout.write("Suspend:    blocked by lease\n")
        if leases is None:
            return
        if not leases.leases:
            return
        rendered = ", ".join(
            f"{lease.lease_id.value} (expires in {_format_duration((lease.expires_at - now).total_seconds())})"
            for lease in leases.leases
        )
        stdout.write(f"Leases:     {rendered}\n")
        if lifecycle.next_transition_at is not None:
            latest_expiry = max(lease.expires_at for lease in leases.leases)
            if latest_expiry >= lifecycle.next_transition_at:
                remaining = (latest_expiry - now).total_seconds()
                stdout.write(f"Grace:      starts after latest lease expiry in {_format_duration(remaining)}\n")
            else:
                remaining = (lifecycle.next_transition_at - now).total_seconds()
                stdout.write(f"Suspend in: {_format_duration(remaining)}\n")
        return
    if blockers:
        stdout.write(f"Suspend:    blocked by {', '.join(blockers)}\n")
        return
    if lifecycle.state.value == "idle_timing" and lifecycle.next_transition_at is not None:
        remaining = (lifecycle.next_transition_at - now).total_seconds()
        stdout.write(f"Suspend in: {_format_duration(remaining)}\n")
    elif lifecycle.state.value == "pending_grace":
        stdout.write("Suspend:    grace period\n")
    elif lifecycle.state.value == "suspend_requested":
        stdout.write("Suspend:    requested\n")
    else:
        stdout.write("Suspend:    waiting for inactivity\n")


def render_text(parsed: argparse.Namespace, value: object, stdout: TextIO, *, now: datetime | None = None) -> None:
    current_time = now or datetime.now(timezone.utc)
    if parsed.command == "status":
        if isinstance(value, StatusLeaseDetails):
            _render_status(value.status, stdout, now=current_time, leases=value.leases)
        else:
            _render_status(value, stdout, now=current_time)
    elif parsed.command == "profile" and parsed.profile_command == "list":
        catalog: ProfileCatalog = value
        selected = catalog.reconciliation.name
        stdout.write("Profiles:\n")
        for name in catalog.names:
            marker = "* " if name == selected else "  "
            stdout.write(f"{marker}{name}\n")
    elif parsed.command == "profile" and parsed.profile_command == "apply":
        application: ProfileApplication = value
        stdout.write(f"Profile {application.outcome.value}: {application.name.value}\n")
        stdout.write(f"CPU: {application.cpu.state.value}\n")
        stdout.write(f"GPU: {application.gpu.state.value}\n")
    elif parsed.command == "suspend":
        receipt: SuspendReceipt = value
        stdout.write(f"Suspend {receipt.outcome.value}\n")
        if receipt.outcome.value != "accepted":
            stdout.write(f"Can suspend: {receipt.can_suspend.value}\n")
            if receipt.blockers:
                stdout.write("Blockers: " + ", ".join(blocker.what for blocker in receipt.blockers) + "\n")
    elif parsed.command == "wake":
        result: WakeResult = value
        stdout.write("Wake complete\n")
        stdout.write(f"Service:    {result.service_status.service_state.value}\n")
        stdout.write(f"Elapsed:    {_format_duration(result.elapsed_seconds)}\n")
        for probe in result.probe_results:
            stdout.write(f"Probe:      {probe.probe_identity} ({probe.state})\n")
    elif parsed.command == "lease" and parsed.lease_command in {"acquire", "renew"}:
        stdout.write(f"Lease {parsed.lease_command}d\n")
        stdout.write(f"ID:      {value.lease_id.value}\n")
        stdout.write(f"TTL:     {value.ttl_seconds}s\n")
        stdout.write(f"Expires: {_format_timestamp(value.expires_at)}\n")
    elif parsed.command == "lease" and parsed.lease_command == "release":
        stdout.write(f"Lease released: {value['lease_id']}\n")
    elif parsed.command == "lease" and parsed.lease_command == "list":
        collection: LeaseCollection = value
        if not collection.leases:
            stdout.write("No active leases.\n")
            return
        stdout.write(f"{'ID':36}  {'Acquired':8}  {'Duration':8}  Expires\n")
        for lease in collection.leases:
            stdout.write(f"{lease.lease_id.value:36}  {'—':8}  {_format_duration(lease.ttl_seconds):8}  {_format_timestamp(lease.expires_at)}\n")


def _write_error(error: PowerClientError, stderr: TextIO) -> int:
    if isinstance(error, ConfigurationError):
        message = f"configuration error: {error.operation}.{error.field}: {error.message}"
        exit_code = 3
    else:
        message = f"{error.__class__.__name__}: "
        if error.__class__.__name__ in {"TransportError", "TlsVerificationError", "RequestTimeoutError"}:
            message += f"{error.cause_category} failure"
        else:
            message += str(error)
        exit_code = 4
    stderr.write(f"powerctl: {message}\n")
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        parsed = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)
    try:
        config_path = resolve_config_path(getattr(parsed, "config", None), os.environ)
        configuration = load_cli_configuration(config_path)
        result = asyncio.run(run_command(parsed, configuration))
        if parsed.output == "json":
            render_success(result, sys.stdout)
        else:
            render_text(parsed, result, sys.stdout)
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except ConfigurationError as error:
        return _write_error(error, sys.stderr)
    except PowerClientError as error:
        return _write_error(error, sys.stderr)
    except Exception:
        sys.stderr.write("powerctl: unexpected error\n")
        return 1
