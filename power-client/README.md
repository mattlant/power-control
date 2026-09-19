# power-client

`power-client` is the client-side component of [power-control](../README.md).

It provides:

* a typed asynchronous Python client for `power-service`;
* the `powerctl` command-line interface;
* Wake-on-LAN support;
* optional workload-readiness checks;
* typed status, profile, suspend, and lease operations.

For normal interactive use, use the `powerctl` CLI. The Python API is available for integration into other applications.

## Installation

From the `power-client` project directory:

```bash
python -m venv .venv
```

Activate the virtual environment.

Linux:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install:

```bash
python -m pip install .
```

This installs:

```text
powerctl
```

On Windows the executable is `powerctl.exe`.

For the complete service and client deployment procedure, including credentials and TLS trust, see:

[Manual Installation and Deployment Guide](../docs/manual-installation-deployment-guide.md)

## CLI

General form:

```text
powerctl [--config <config.toml>] [--output text|json] <command>
```

Supported operations include:

```text
powerctl status

powerctl profile list
powerctl profile apply <name>

powerctl suspend

powerctl lease list
powerctl lease acquire <ttl-seconds>
powerctl lease renew <lease-id> <ttl-seconds>
powerctl lease release <lease-id>

powerctl wake
powerctl wake <readiness-target> [expected-http-status]
```

Human-readable output is used by default.

For scripting:

```bash
powerctl --output json status
```

JSON output exposes the complete client-side DTO for the requested operation.

## Configuration

The client loads configuration in this order:

```text
--config <path>
POWERCTL_CONFIG
$XDG_CONFIG_HOME/power-client/config.toml
~/.config/power-client/config.toml
```

If an explicitly selected configuration file is missing or invalid, the client does not fall through to another source.

There are no built-in deployment defaults for endpoint, credentials, trust, or timeouts.

A typical configuration looks like:

```toml
[service]
endpoint = "https://power-host.example.test:9443"
request_timeout = 5.0
credential_file = "/absolute/path/to/power-service-control"
# additional_ca_bundle_path = "/absolute/path/to/public-ca.pem"

[wake]
mac_address = "AA:BB:CC:DD:EE:FF"
broadcast_address = "192.168.1.255"
udp_port = 9
service_wait_timeout = 60.0
service_wait_poll_interval = 2.0
```

The `[wake]` section is required only when using `powerctl wake`.

The service endpoint must be an HTTPS origin only:

```text
https://host:port
```

Do not include an API path, query string, fragment, or credentials.

## Credentials

The client reads bearer credentials from a protected file.

The file contains one credential:

```text
credential-id.secret
```

For example:

```text
power-service-control.<secret>
```

The credential file must be an absolute regular file.

On POSIX systems, group and world permissions are rejected.

Recommended permissions:

```bash
chmod 600 ~/.config/power-client/credentials/power-service-control
```

On Windows, the credential ACL must be restricted to the owning user and permitted system/administrator principals.

Credentials are never supplied directly through command-line arguments.

## TLS

TLS certificate and hostname verification are always enabled.

By default, the client uses the operating system trust store.

For deployments using a private CA, install the public issuing CA into the operating system trust store and leave:

```toml
# additional_ca_bundle_path = "..."
```

unset.

An additional public CA bundle may also be configured explicitly:

```toml
additional_ca_bundle_path = "/absolute/path/to/public-ca.pem"
```

This extends platform trust rather than disabling verification.

A server leaf certificate is not treated as a trust anchor.

## Status

Retrieve service and host state:

```bash
powerctl status
```

Status includes information such as:

* configured profile state;
* CPU policy state and capabilities;
* GPU status and power limits;
* suspend availability;
* automatic-suspend lifecycle state;
* active blockers;
* lease information;
* relevant activity state.

For the full structured result:

```bash
powerctl --output json status
```

## Profiles

List configured profiles:

```bash
powerctl profile list
```

Apply a profile:

```bash
powerctl profile apply balanced
```

Profile names must match:

```text
[A-Za-z0-9_-]{1,64}
```

The service remains authoritative for profile validation and application.

## Suspend

Request direct host suspend:

```bash
powerctl suspend
```

The client does not retry suspend requests automatically.

If the request outcome becomes ambiguous because of transport failure or cancellation, the client does not infer whether the host applied the operation. Callers should inspect current service/host state according to their own policy.

## Leases

List active leases:

```bash
powerctl lease list
```

`Duration` is the configured lease duration (`ttl_seconds`), not a decreasing remaining-TTL value; `Expires` is the authoritative expiry projection.

Acquire a lease:

```bash
powerctl lease acquire 300
```

Renew:

```bash
powerctl lease renew <lease-id> 300
```

Release:

```bash
powerctl lease release <lease-id>
```

Lease lifecycle is caller-owned.

The client does not automatically:

* renew leases;
* release leases;
* retry lease mutations;
* run background lease-management tasks;
* infer automatic-suspend policy.

The service remains authoritative for effective TTL and expiry.

## Wake-on-LAN

With `[wake]` configured:

```bash
powerctl wake
```

The client:

```text
send one configured Wake-on-LAN magic packet
    ↓
wait for authenticated power-service readiness
    ↓
optionally run workload-readiness probes
```

Wake configuration includes:

```toml
[wake]
mac_address = "AA:BB:CC:DD:EE:FF"
broadcast_address = "192.168.1.255"
udp_port = 9
service_wait_timeout = 60.0
service_wait_poll_interval = 2.0
```

## Workload readiness

Optional workload probes can run after `power-service` itself becomes reachable.

Supported probe types are:

* TCP connection;
* HTTP/HTTPS status.

Example:

```toml
[wake]
mac_address = "AA:BB:CC:DD:EE:FF"
broadcast_address = "192.168.1.255"
udp_port = 9

service_wait_timeout = 60.0
service_wait_poll_interval = 2.0

readiness_wait_timeout = 120.0
readiness_attempt_timeout = 5.0
readiness_wait_poll_interval = 2.0

[[wake.probe]]
identity = "application-http"
type = "http"
url = "https://application.example.test/health"
expected_statuses = [200]

[[wake.probe]]
identity = "application-tcp"
type = "tcp"
host = "application.example.test"
port = 443
```

Probes run in declared order under one bounded readiness deadline.

Transient connection or unexpected-status failures are retried until the readiness deadline expires.

HTTPS verification remains enabled. Workload probes do not receive the `power-service` bearer credential.

### Ad-hoc readiness target

A one-off target may be supplied directly:

```bash
powerctl wake tcp://application.example.test:443
```

or:

```bash
powerctl wake https://application.example.test/health
```

An expected HTTP status may optionally be supplied:

```bash
powerctl wake https://application.example.test/health 204
```

The ad-hoc target replaces configured workload probes for that invocation.

## Python API

The same functionality is available through the typed asynchronous client.

Example:

```python
import asyncio
from pathlib import Path

from power_client import (
    CredentialFileReference,
    ProfileName,
    ServiceConnectionConfig,
    TrustConfig,
    compose_client,
)


async def main() -> None:
    connection = ServiceConnectionConfig(
        endpoint="https://power-host.example.test:9443",
        request_timeout=5.0,
        trust=TrustConfig(),
    )

    credential = CredentialFileReference(
        Path("/absolute/path/to/power-service-control")
    )

    client = compose_client(connection, credential)

    async with client:
        status = await client.get_status()
        profiles = await client.list_profiles()
        result = await client.apply_profile(ProfileName("balanced"))

        print(status)
        print(profiles)
        print(result)


asyncio.run(main())
```

Use `async with` so the client-owned transport is closed correctly.

An additional CA bundle can be supplied when required:

```python
trust = TrustConfig(
    additional_ca_bundle_path=Path("/absolute/path/to/public-ca.pem")
)
```

## Errors

Client failures are exposed as typed errors from `power_client.errors`.

These include failures related to:

* authentication;
* authorization;
* API responses;
* protocol validation;
* TLS verification;
* timeout;
* configuration.

Diagnostic output avoids bearer credentials, TLS private material, and raw sensitive response content.

## Exit codes

`powerctl` uses:

| Exit code | Meaning                        |
| --------: | ------------------------------ |
|       `0` | Success                        |
|       `1` | Unexpected client failure      |
|       `2` | Invalid command syntax         |
|       `3` | Configuration error            |
|       `4` | Client/service operation error |
|     `130` | Cancellation                   |

## Development

Create and activate a virtual environment, then install with test dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

Run the test suite:

```bash
python -m unittest discover -s tests -v
```

The project uses a `src/` layout:

```text
power-client/
├── src/
│   └── power_client/
├── tests/
├── pyproject.toml
└── README.md
```

## Related component

[power-service](../power-service/README.md) provides the authenticated Linux host service operated by this client.
