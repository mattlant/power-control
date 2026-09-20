# power-control

**Wake it when you need it. Let it sleep when you don’t.**

`power-control` is a remote power-management toolkit for Linux hosts.

It provides a hardened host service and a cross-platform client for reading power state, applying CPU/GPU profiles, managing suspend-prevention leases, suspending hosts, waking them with Wake-on-LAN, and waiting for workloads to become ready.

It was originally built for a local AI inference setup, where leaving a high-power GPU machine awake indefinitely just in case another request arrives is wasteful. The same primitives are useful anywhere a machine is powerful, occasionally needed, and expensive to leave fully awake.

## Why power-control?

High-performance machines are great when they are doing useful work.

They are considerably less impressive when they are sitting idle for hours consuming power, generating heat, spinning fans, and waiting for something that may or may not happen.

`power-control` makes power state part of the system rather than something a human has to remember to manage manually.

A remote machine can be:

```text
asleep
  ↓
needed by a workload
  ↓
wake
  ↓
wait until the host is reachable
  ↓
wait until the workload is ready
  ↓
perform work while suspend is blocked
  ↓
release the lease
  ↓
idle policy takes over
  ↓
suspend
```

The goal is simple: use powerful hardware when it is useful and stop paying the power, heat, noise, and environmental cost when it is not. 😁

## What it does

`power-control` currently supports:

* authenticated remote host status;
* named CPU/GPU power profiles;
* CPU frequency, governor, and Energy Performance Preference control;
* NVIDIA GPU power-limit control;
* direct host suspend;
* automatic idle suspend;
* suspend-prevention leases;
* local workload activity detection;
* interactive terminal activity detection;
* Wake-on-LAN;
* service-readiness waiting;
* generic HTTP/HTTPS and TCP workload-readiness probes;
* human-readable and JSON CLI output;
* typed asynchronous Python client access.

Power policy remains deployment-owned. The software provides the mechanisms; the operator decides what profiles, idle periods, workload signals, credentials, and readiness rules make sense for the machine.

## Architecture

The project consists of two independently installable Python components:

```text
power-control/
├── power-service/
│   └── Linux host service
│
└── power-client/
    ├── typed async Python client
    └── powerctl CLI
```

At runtime:

```text
powerctl / Python application
          │
          │ HTTPS + bearer authentication
          ▼
   power-service-api
          │
          │ protected Unix socket
          ▼
 power-service-broker
          │
          ├── CPUFreq
          ├── NVIDIA GPU
          └── systemd-logind
```

The network-facing API does not directly own privileged host-control capabilities.

The dedicated broker performs host operations and exposes no TCP listener.

See:

* [power-service](power-service/README.md)
* [power-client](power-client/README.md)

## Example use cases

### Local AI inference

This was the original motivation for the project.

A GPU inference machine can remain suspended until a request actually requires it:

```text
LLM request
    │
    ▼
orchestrator / proxy
    │
    ├── wake inference host
    ├── wait for power-service
    ├── wait for inference workload
    ├── acquire or maintain a lease
    │
    ▼
run inference
    │
    ▼
release lease
    │
    ▼
automatic idle policy
    │
    ▼
suspend
```

An upcoming `llm-proxy` project will use `power-control` this way in my local inference environment.

`llm-proxy` is not required to use this project. The service and client are intentionally generic.

### Home lab servers

Keep machines asleep until a developer, service, automation task, or scheduled job actually needs them.

Wake the host remotely, wait until required services are ready, perform the work, and let automatic idle policy suspend it afterward.

### Build and CI workers

A high-power build machine does not necessarily need to run 24/7.

Automation can:

```text
wake
→ wait for SSH / runner / build service
→ acquire lease
→ run build
→ release lease
→ allow suspend
```

### Rendering and media workloads

Rendering, encoding, transcoding, and batch-processing machines can be brought online only for active workloads.

### Remote workstations

Use Wake-on-LAN, readiness checks, power profiles, and suspend to manage a workstation that spends much of its time unattended.

### Scheduled automation

Cron jobs, systemd timers, home automation, or other orchestrators can treat host availability as part of the workflow rather than assuming the machine is always running.

### Energy-aware workloads

Applications can select deployment-defined profiles such as:

```text
max
balanced
balanced-quiet
power-save
```

based on workload demand.

The names and policies themselves are operator-defined.

## Leases

Leases are one of the central coordination mechanisms in `power-control`.

A lease means:

> something currently needs this machine; automatic suspend must wait.

For example:

```text
application needs host
    ↓
wake
    ↓
acquire lease
    ↓
perform work
    ↓
release lease
    ↓
normal idle policy resumes
```

Leases have bounded lifetimes and expire automatically.

They are deliberately separate from activity detection. Holding a lease prevents suspend but does not pretend that real workload activity occurred or reset the host's stable-idle timer.

This allows multiple clients and services to coordinate around the same host lifecycle without each one having to own suspend policy.

## Automatic suspend

`power-service` can automatically suspend a host after a configured stable-idle period and grace period.

The lifecycle distinguishes between **activity** and **eligibility blockers**.

```text
actual activity
    → advances/reset idle timing

eligibility blocker
    → prevents suspend
    → does not reset idle timing
```

Activity can include:

* configured local workload probes;
* recent interactive terminal activity.

Eligibility blockers can include:

* active leases;
* logind `sleep:block` inhibitors;
* active broker operations;
* unavailable suspend capability.

This distinction allows the host to remain safely blocked while something needs it without repeatedly restarting the entire idle countdown.

## Wake and readiness

`power-client` can send a Wake-on-LAN packet and wait until `power-service` is reachable.

It can then optionally wait for actual workload readiness.

Supported readiness probes include:

* TCP connection;
* HTTP/HTTPS status.

For example:

```text
wake host
    ↓
power-service becomes reachable
    ↓
SSH becomes reachable
    ↓
inference API becomes healthy
    ↓
host is ready for work
```

The same mechanism works for arbitrary services and is not tied to AI workloads.

## Power profiles

Profiles combine host-specific CPU and GPU settings under a named policy.

A profile can control:

* CPU policy IDs;
* scaling governor;
* minimum frequency;
* maximum frequency;
* Energy Performance Preference;
* NVIDIA GPU power limit.

For example:

```text
max
    CPU: maximum configured performance
    GPU: maximum configured power limit

balanced
    CPU: dynamic frequency range
    GPU: higher power allowance

balanced-quiet
    CPU: dynamic frequency range
    GPU: reduced power allowance
```

These are examples only.

`power-control` does not decide what "balanced", "quiet", or "maximum" should mean on a particular machine. Profiles are explicit deployment configuration.

## Security model

Remote host power control deserves a narrower security boundary than "run a shell command over the network."

`power-control` separates network access from privileged host control.

### Network API

`power-service-api`:

* runs as a dedicated unprivileged account;
* accepts authenticated HTTPS requests;
* uses permission-scoped bearer credentials;
* communicates with the broker over a protected Unix-domain socket.

### Host-control broker

`power-service-broker`:

* runs under a dedicated service identity;
* exposes no TCP listener;
* receives requests only from the local API boundary;
* operates with bounded filesystem access and Linux capabilities;
* uses normal systemd-logind and PolicyKit authorization for suspend.

### TLS

TLS certificate and hostname verification remain enabled.

The normal deployment model uses a CA-issued service certificate with the issuing CA trusted by client machines.

### Credentials

Credentials can independently grant:

| Permission | Capability                       |
| ---------- | -------------------------------- |
| `status`   | Read host and service state      |
| `profile`  | Apply configured power profiles  |
| `suspend`  | Request direct suspend           |
| `lease`    | Manage suspend-prevention leases |

Bearer secrets are stored only by authorized clients. The service stores salted scrypt hashes.

## CLI examples

Check host status:

```bash
powerctl status
```

List profiles:

```bash
powerctl profile list
```

Apply a profile:

```bash
powerctl profile apply balanced
```

List active leases:

```bash
powerctl lease list
```

Acquire a five-minute lease:

```bash
powerctl lease acquire 5m
```

Lease durations may be specified as seconds or with `h`, `m`, and `s` suffixes;
ordered combinations such as `3h30m` are supported.

Suspend the host:

```bash
powerctl suspend
```

Wake it again:

```bash
powerctl wake
```

Wake it and wait for an application:

```bash
powerctl wake https://application.example.test/health
```

Machine-readable output is also available:

```bash
powerctl --output json status
```

See the [power-client README](power-client/README.md) for client and CLI details.

## Installation

Installation is currently manual.

The full service and client procedure is documented in:

[Manual Installation and Deployment Guide](docs/manual-installation-deployment-guide.md)

The guide covers:

* host prerequisites;
* service identities;
* Python package installation;
* service configuration;
* TLS;
* credentials;
* PolicyKit;
* systemd;
* client configuration;
* CA trust;
* verification;
* update procedures.

Automated installation and deployment tooling is planned as a follow-up.

## Development

The service and client are independent Python projects.

Install either component from its own directory.

For development:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

Run its test suite with:

```bash
python -m unittest discover -s tests -v
```

See the component READMEs for project-specific details.

## Project status

`power-control` is usable today and currently includes:

* [x] authenticated HTTPS host service
* [x] hardened API/broker privilege separation
* [x] CPU/GPU power profiles
* [x] direct suspend
* [x] automatic idle suspend
* [x] suspend-prevention leases
* [x] local activity detection
* [x] interactive terminal activity detection
* [x] Wake-on-LAN
* [x] service-readiness waiting
* [x] generic workload-readiness probes
* [x] Linux and Windows client operation
* [x] typed Python client
* [x] `powerctl` CLI
* [x] manual installation and deployment guide
* [x] list leases from CLI
* [ ] automated install/update/uninstall tooling
* [ ] improved configuration/bootstrap tooling
* [ ] `llm-proxy` integration

The project is being developed primarily for real use in a local compute environment, with reusable behavior kept generic where practical.

## Repository layout

```text
power-control/
├── docs/
│   └── manual-installation-deployment-guide.md
│
├── power-client/
│   ├── src/
│   ├── tests/
│   ├── pyproject.toml
│   └── README.md
│
├── power-service/
│   ├── config/
│   ├── src/
│   ├── systemd/
│   ├── tests/
│   ├── pyproject.toml
│   └── README.md
│
├── LICENSE
└── README.md
```

## License

`power-control` is released under the [MIT License](LICENSE).
