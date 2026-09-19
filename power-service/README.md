# power-service

`power-service` is the Linux host-side component of [power-control](../README.md).

It exposes authenticated host power-management capabilities over HTTPS while isolating privileged host operations behind a dedicated local broker.

For normal remote operation, use [power-client](../power-client/README.md).

## Architecture

`power-service` runs as two separate system services:

```text
remote client
    │
    │ HTTPS
    ▼
power-service-api
    │
    │ Unix domain socket
    ▼
power-service-broker
    │
    ├── CPU frequency control
    ├── NVIDIA GPU power control
    └── systemd-logind
```

### API

`power-service-api` provides the authenticated HTTPS interface.

It runs as a dedicated unprivileged service identity and does not perform privileged host-control operations directly.

### Broker

`power-service-broker` owns host-control operations.

It exposes no TCP listener. Communication from the API uses a private Unix-domain socket with peer validation and restricted filesystem permissions.

The supplied systemd and PolicyKit assets constrain the broker's host access to the capabilities required by the service.

## Capabilities

### Host status

Status includes the service's current view of:

* CPU policies and configured frequency ranges;
* GPU identity, driver and power limits;
* configured power profiles;
* suspend capability and inhibitors;
* automatic-suspend lifecycle state;
* active lifecycle blockers;
* lease summaries and expiration;
* relevant activity state.

### Power profiles

Profiles combine configured CPU and GPU settings under a named policy.

CPU configuration can include:

* CPU policy IDs;
* scaling governor;
* minimum and maximum frequency;
* Energy Performance Preference (EPP).

GPU configuration specifies the NVIDIA power limit for the selected GPU.

Profile values are validated against the capabilities discovered from the host before they are applied.

Profiles are deployment-specific and are defined in `broker.toml`.

### Direct suspend

Authorized clients can request host suspend.

Suspend is coordinated through `systemd-logind`; the service does not bypass normal logind authorization or `sleep:block` inhibitors.

A successful suspend request is committed before the host is allowed to enter the suspend lifecycle. Host suspend/resume and transport interruption after commit do not retroactively change an accepted request into a failure.

### Leases

Authorized clients can acquire bounded leases that temporarily prevent automatic suspend.

Leases:

* have an operator-configured maximum TTL;
* expire automatically;
* are scoped to the authenticated principal;
* do not survive broker restart or host resume.

Lease acquisition and renewal do not represent host activity and do not reset the stable-idle timer.

### Automatic suspend

The broker can automatically suspend the host after a configured stable-idle period and grace period.

Suspend eligibility can be blocked by conditions including:

* active leases;
* `sleep:block` inhibitors;
* broker mutations;
* unavailable suspend capability;
* configured local activity;
* recent interactive terminal activity.

Activity and eligibility are treated separately:

```text
actual activity
    → advances/reset idle timing

eligibility blocker
    → prevents suspend
    → does not reset idle timing
```

If the host has already satisfied the stable-idle interval when an eligibility blocker clears, a new full grace period begins before suspend is attempted.

### Local activity

Automatic suspend can observe configured local activity probes such as:

* process-name activity;
* TCP-listener activity.

These probes are deployment policy and are configured in `broker.toml`.

### Interactive terminal activity

Optional interactive-session monitoring uses systemd-logind session information and controlling PTY activity to identify recent activity from eligible local-console and SSH terminal sessions.

When enabled, recent terminal activity participates in stable-idle timing. Unavailable or untrustworthy interactive-session state fails safe by preventing automatic suspend.

## Configuration

Service configuration is intentionally external to the Python package.

Typical deployment layout:

```text
/etc/power-service/
├── api.toml
├── broker.toml
└── tls/
    ├── server.crt
    └── server.key
```

Repository templates are provided in [`config/`](config/).

### `api.toml`

API configuration defines:

* HTTPS listener address and port;
* broker socket location;
* TLS certificate and private-key paths;
* request timeout;
* authenticated credentials and permissions.

Credential records contain the credential ID, salt, scrypt hash and granted permissions. Plaintext bearer secrets are held by clients and must not be stored in the service configuration.

### `broker.toml`

Broker configuration defines:

* API peer UID;
* GPU index;
* operation/query bounds;
* automatic-suspend policy;
* local activity probes;
* named CPU/GPU power profiles.

CPU policy IDs, frequency limits, EPP values and GPU power limits are host-specific and must be selected for the target system.

## Authentication and permissions

Clients authenticate with bearer credentials.

Permissions are capability-based:

| Permission | Capability                                           |
| ---------- | ---------------------------------------------------- |
| `status`   | Read status and profile information                  |
| `profile`  | Apply configured power profiles                      |
| `suspend`  | Request direct host suspend                          |
| `lease`    | Acquire, renew and release suspend-prevention leases |

A deployment may create multiple credentials with different permission sets.

## TLS

The HTTPS API requires TLS.

The normal deployment model is a server certificate issued by a CA trusted by the client host. Certificate and hostname verification remain enabled by the client.

TLS private keys and bearer credentials must remain outside the repository.

## Installation and deployment

For the complete host installation, configuration, TLS, service-account, PolicyKit, systemd and verification procedure, see:

[Manual Installation and Deployment Guide](../docs/manual-installation-deployment-guide.md)

The deployment installs two systemd units:

```text
power-service-broker.service
power-service-api.service
```

and the accompanying assets under [`systemd/`](systemd/), including:

* the broker PolicyKit rule;
* the post-resume broker reconciliation hook.

The broker should be started before the API. The supplied systemd units define their dependency relationship for normal startup.

## Development

Create a virtual environment and install the project with test dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

Run the test suite:

```bash
python -m unittest discover -s tests -v
```

The service is packaged with a `src/` layout:

```text
power-service/
├── config/
├── src/
│   └── power_service/
├── systemd/
├── tests/
├── pyproject.toml
└── README.md
```

## Related component

[power-client](../power-client/README.md) provides the typed Python client and `powerctl` CLI used to operate the service remotely.
