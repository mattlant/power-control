<a id="top"></a>

# Power Control — Manual Installation and Deployment

This document defines the manual installation procedure for the `power-control` repository.

The repository contains two independently installable Python projects:

```text
power-control/
├── power-service/
└── power-client/
```

`power-service` runs on the controlled Linux host. `power-client` may run on Linux or Windows and communicates with the service over authenticated HTTPS.

This procedure intentionally keeps machine-specific configuration, credentials, TLS material, CPU/GPU policy, and client Wake-on-LAN settings outside the installed Python packages.

## Table of Contents

* [1. Architecture](#1-architecture)
* [2. Service host prerequisites](#2-service-host-prerequisites)
* [3. Create service identities](#3-create-service-identities)
* [4. Install power-service](#4-install-power-service)
* [5. Create service configuration directories](#5-create-service-configuration-directories)
* [6. Provision TLS](#6-provision-tls)
* [7. Generate bearer credentials](#7-generate-bearer-credentials)
* [8. Configure the API](#8-configure-the-api)
* [9. Configure the broker](#9-configure-the-broker)
* [10. Install the PolicyKit rule](#10-install-the-policykit-rule)
* [11. Install the resume reconciliation hook](#11-install-the-resume-reconciliation-hook)
* [12. Install systemd units](#12-install-systemd-units)
* [13. Enable and start power-service](#13-enable-and-start-power-service)
* [14. Verify the service installation](#14-verify-the-service-installation)
* [15. Install power-client](#15-install-power-client)
* [16. Install client credentials](#16-install-client-credentials)
* [17. Configure power-client](#17-configure-power-client)
* [18. Configure client CA trust](#18-configure-client-ca-trust)
* [19. Verify power-client](#19-verify-power-client)
* [20. Updating power-service](#20-updating-power-service)
* [21. Updating power-client](#21-updating-power-client)
* [22. Configuration-only changes](#22-configuration-only-changes)
* [23. Installed filesystem layout](#23-installed-filesystem-layout)
* [24. Machine-specific example configuration](#24-machine-specific-example-configuration)

---

# 1. Architecture

The service separates network access from privileged host control:

```text
power-client
    │
    │ HTTPS + bearer credential
    ▼
power-service-api
    │
    │ private Unix socket
    ▼
power-service-broker
    │
    ├── CPU frequency control
    ├── NVIDIA GPU power control
    ├── systemd-logind suspend
    ├── leases
    └── automatic suspend lifecycle
```

The API and broker run under separate dedicated Linux identities.

```text
power-service-api
    unprivileged HTTPS/API process

power-service-broker
    dedicated host-control process
    bounded Linux capabilities
    bounded writable filesystem access
    PolicyKit authorization for suspend
```

The broker has no TCP listener.

[Back to top](#top)

---

# 2. Service host prerequisites

The controlled Linux host requires:

```text
Python 3.11 or later
Python venv support
systemd
systemd-logind
PolicyKit
OpenSSL
busctl
```

CPU profile control requires:

```text
cpupower
```

NVIDIA GPU profile control requires:

```text
nvidia-smi
```

Verify:

```bash
for cmd in \
  python3 \
  openssl \
  busctl \
  cpupower \
  nvidia-smi
do
  printf '%-12s ' "$cmd"
  command -v "$cmd" || echo MISSING
done
```

Verify Python virtual-environment support:

```bash
python3 -m venv --help >/dev/null && echo "python venv: OK"
```

`cpupower` and `nvidia-smi` are host/hardware-specific dependencies. A deployment that does not use the corresponding functionality may have different requirements.

[Back to top](#top)

---

# 3. Create service identities

Create the API identity:

```bash
sudo useradd \
  --system \
  --user-group \
  --home-dir /nonexistent \
  --no-create-home \
  --shell /usr/sbin/nologin \
  power-service-api
```

Create the broker identity:

```bash
sudo useradd \
  --system \
  --user-group \
  --home-dir /home/power-service-broker \
  --create-home \
  --shell /usr/sbin/nologin \
  power-service-broker
```

Add the broker to the API supplementary group:

```bash
sudo usermod \
  --append \
  --groups power-service-api \
  power-service-broker
```

Verify:

```bash
id power-service-api
id power-service-broker
```

Record the numeric UID assigned to `power-service-api`:

```bash
id -u power-service-api
```

That numeric UID is required by `broker.toml`.

Do not assume a particular UID or GID on a new machine.

[Back to top](#top)

---

# 4. Install power-service

From the `power-service` project:

```bash
cd power-control/power-service
```

Create the dedicated service virtual environment:

```bash
sudo python3 -m venv /opt/power-service
```

Install the package:

```bash
sudo /opt/power-service/bin/pip install .
```

Expose the installed commands at the paths expected by the systemd units:

```bash
sudo ln -sf \
  /opt/power-service/bin/power-service-api \
  /usr/local/bin/power-service-api

sudo ln -sf \
  /opt/power-service/bin/power-service-broker \
  /usr/local/bin/power-service-broker
```

Verify:

```bash
/usr/local/bin/power-service-api --help
/usr/local/bin/power-service-broker --help
```

The installed Python package lives under `/opt/power-service`; repository files are not executed directly by systemd.

[Back to top](#top)

---

# 5. Create service configuration directories

Create the main configuration directory:

```bash
sudo install -d \
  -o root \
  -g power-service-api \
  -m 0750 \
  /etc/power-service
```

Create the TLS directory:

```bash
sudo install -d \
  -o root \
  -g power-service-api \
  -m 0750 \
  /etc/power-service/tls
```

The target layout is:

```text
/etc/power-service/
├── api.toml
├── broker.toml
└── tls/
    ├── server.crt
    └── server.key
```

The runtime directory is **not** created manually during installation.

systemd creates:

```text
/run/power-service
```

through the broker unit's `RuntimeDirectory=` configuration.

[Back to top](#top)

---

# 6. Provision TLS

The normal deployment model is a CA-issued server certificate.

```text
service host
    ├── generates server private key
    └── generates CSR

trusted CA
    └── signs CSR

service host
    ├── installs issued server certificate
    └── retains private key

client
    └── trusts issuing CA
```

The CA private key must never be copied to the power-service host or client hosts.

## Generate the service private key

On the service host:

```bash
openssl genrsa -out server.key 2048
chmod 600 server.key
```

## Create a CSR configuration

Example:

```ini
[req]
distinguished_name = dn
req_extensions = req_ext
prompt = no

[dn]
CN = <SERVICE_HOSTNAME>

[req_ext]
subjectAltName = @alt_names

[alt_names]
DNS.1 = <SERVICE_HOSTNAME>
DNS.2 = <OPTIONAL_SHORT_HOSTNAME>
IP.1 = <SERVICE_IP>
```

Generate the CSR:

```bash
openssl req -new \
  -key server.key \
  -out server.csr \
  -config server.cnf
```

Have the trusted CA sign `server.csr`.

The resulting certificate must contain the DNS names and/or IP addresses clients will actually use.

## Verify the issued certificate

Verify its identity:

```bash
openssl x509 \
  -in server.crt \
  -noout \
  -subject \
  -issuer \
  -dates \
  -ext subjectAltName
```

Verify the certificate and private key correspond.

Certificate public key:

```bash
openssl x509 \
  -in server.crt \
  -noout -pubkey | \
  openssl pkey -pubin -outform pem | \
  sha256sum
```

Private-key-derived public key:

```bash
openssl pkey \
  -in server.key \
  -pubout -outform pem | \
  sha256sum
```

The hashes must match.

## Install the certificate

```bash
sudo install \
  -o root \
  -g power-service-api \
  -m 0644 \
  server.crt \
  /etc/power-service/tls/server.crt
```

Install the private key:

```bash
sudo install \
  -o root \
  -g power-service-api \
  -m 0640 \
  server.key \
  /etc/power-service/tls/server.key
```

Verify:

```bash
sudo stat -c '%U:%G %a %n' \
  /etc/power-service/tls/server.crt \
  /etc/power-service/tls/server.key
```

Expected:

```text
root:power-service-api 644 /etc/power-service/tls/server.crt
root:power-service-api 640 /etc/power-service/tls/server.key
```

[Back to top](#top)

---

# 7. Generate bearer credentials

A typical deployment uses two credentials:

| Credential              | Permissions                             |
| ----------------------- | --------------------------------------- |
| `power-service-status`  | `status`                                |
| `power-service-control` | `status`, `profile`, `suspend`, `lease` |

Generate them without `sudo`:

```bash
python3 - <<'PY'
import hashlib
import secrets

for credential_id in ("power-service-status", "power-service-control"):
    secret = secrets.token_urlsafe(32)
    salt = secrets.token_bytes(16)

    digest = hashlib.scrypt(
        secret.encode(),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )

    print(f"{credential_id}:")
    print(f"  bearer: {credential_id}.{secret}")
    print(f"  salt_hex: {salt.hex()}")
    print(f"  scrypt_hash_hex: {digest.hex()}")
    print()
PY
```

The bearer credential format is:

```text
<credential-id>.<secret>
```

For example:

```text
power-service-control.<secret>
```

Store the plaintext bearer securely for installation on authorized clients.

The server stores only:

```text
credential ID
salt
scrypt hash
permissions
```

Do not store plaintext bearer credentials in:

```text
Git
server configuration
documentation
test output
logs
```

[Back to top](#top)

---

# 8. Configure the API

Create:

```text
/etc/power-service/api.toml
```

Example:

```toml
listen_host = "<PRIVATE_LISTENER_ADDRESS>"
listen_port = 9443
broker_socket = "/run/power-service/broker.sock"
tls_cert = "/etc/power-service/tls/server.crt"
tls_key = "/etc/power-service/tls/server.key"
request_timeout_seconds = 5

[[credentials]]
id = "power-service-status"
salt_hex = "<STATUS_SALT>"
scrypt_hash_hex = "<STATUS_HASH>"
permissions = ["status"]

[[credentials]]
id = "power-service-control"
salt_hex = "<CONTROL_SALT>"
scrypt_hash_hex = "<CONTROL_HASH>"
permissions = ["status", "profile", "suspend", "lease"]
```

Install with:

```bash
sudo chown \
  root:power-service-api \
  /etc/power-service/api.toml

sudo chmod \
  0640 \
  /etc/power-service/api.toml
```

Verify:

```bash
sudo stat -c '%U:%G %a %n' \
  /etc/power-service/api.toml
```

Expected:

```text
root:power-service-api 640 /etc/power-service/api.toml
```

[Back to top](#top)

---

# 9. Configure the broker

Create:

```text
/etc/power-service/broker.toml
```

Start from the repository template.

Core settings include:

```toml
socket_path = "/run/power-service/broker.sock"
api_uid = <POWER_SERVICE_API_UID>
gpu_index = 0
query_timeout_seconds = 5
operation_timeout_seconds = 5
```

Replace:

```text
<POWER_SERVICE_API_UID>
```

with:

```bash
id -u power-service-api
```

## Automatic suspend

Example structure:

```toml
[automatic_suspend]
enabled = true
interactive_sessions_enabled = true
interactive_activity_timeout_seconds = 30
max_lease_ttl_seconds = 3600
stable_idle_seconds = 10800
grace_seconds = 120
evaluation_interval_seconds = 10
max_status_principals = 20
local_activity_probes = []
```

These values are deployment policy, not universal defaults.

## Profiles

CPU and GPU profiles are machine-specific.

Example structure:

```toml
[[profiles]]
name = "balanced"

[profiles.cpu]
policy_ids = [0, 1]
governor = "powersave"
min_khz = 1000000
max_khz = 3000000
epp = "balance_performance"

[profiles.gpu]
power_limit_w = "250"
```

Determine valid values from the actual host.

Do not blindly copy policy IDs, CPU frequencies, EPP values, or GPU power limits from another machine.

Install permissions:

```bash
sudo chown \
  root:root \
  /etc/power-service/broker.toml

sudo chmod \
  0600 \
  /etc/power-service/broker.toml
```

Verify:

```bash
sudo stat -c '%U:%G %a %n' \
  /etc/power-service/broker.toml
```

Expected:

```text
root:root 600 /etc/power-service/broker.toml
```

[Back to top](#top)

---

# 10. Install the PolicyKit rule

Install:

```text
power-service/systemd/49-power-service-broker.rules
```

to:

```text
/etc/polkit-1/rules.d/49-power-service-broker.rules
```

Command:

```bash
sudo install \
  -o root \
  -g root \
  -m 0644 \
  systemd/49-power-service-broker.rules \
  /etc/polkit-1/rules.d/49-power-service-broker.rules
```

The rule authorizes only the dedicated broker identity for the required logind suspend operations.

Expected rule:

```javascript
polkit.addRule(function(action, subject) {
    if (subject.user == "power-service-broker" &&
        (action.id == "org.freedesktop.login1.suspend" ||
         action.id == "org.freedesktop.login1.suspend-multiple-sessions")) {
        return polkit.Result.YES;
    }
});
```

It must not grant broad login1 privileges or `ignore-inhibit`.

Verify:

```bash
sudo diff -u \
  systemd/49-power-service-broker.rules \
  /etc/polkit-1/rules.d/49-power-service-broker.rules
```

No output means the installed rule matches the repository version.

[Back to top](#top)

---

# 11. Install the resume reconciliation hook

The broker must reconcile host state after resume.

Install:

```text
systemd/power-service-reconcile
```

as:

```text
/usr/lib/systemd/system-sleep/power-service-reconcile
```

Command:

```bash
sudo install \
  -o root \
  -g root \
  -m 0755 \
  systemd/power-service-reconcile \
  /usr/lib/systemd/system-sleep/power-service-reconcile
```

Current hook:

```sh
#!/bin/sh

if [ "$1" = "post" ] && [ "$2" = "suspend" ]; then
    exec /bin/systemctl kill --kill-whom=main -s SIGUSR1 power-service-broker.service
fi

exit 0
```

On usr-merged systems, the same file may also appear through:

```text
/lib/systemd/system-sleep/power-service-reconcile
```

Do not install a second independent copy there.

Verify:

```bash
ls -l \
  /usr/lib/systemd/system-sleep/power-service-reconcile
```

Expected mode:

```text
0755
```

[Back to top](#top)

---

# 12. Install systemd units

Install the broker unit:

```bash
sudo install \
  -o root \
  -g root \
  -m 0644 \
  systemd/power-service-broker.service \
  /etc/systemd/system/power-service-broker.service
```

Install the API unit:

```bash
sudo install \
  -o root \
  -g root \
  -m 0644 \
  systemd/power-service-api.service \
  /etc/systemd/system/power-service-api.service
```

Reload systemd:

```bash
sudo systemctl daemon-reload
```

## Broker unit contract

The broker runs as:

```ini
User=power-service-broker
Group=power-service-broker
SupplementaryGroups=power-service-api
```

Its runtime directory is systemd-managed:

```ini
RuntimeDirectory=power-service
RuntimeDirectoryMode=0755
```

Its host-control surface is bounded:

```ini
ProtectSystem=full
ReadWritePaths=/sys/devices/system/cpu/cpufreq
ReadOnlyPaths=/usr/bin/cpupower /usr/bin/nvidia-smi /usr/bin/busctl
AmbientCapabilities=CAP_SYS_ADMIN CAP_DAC_OVERRIDE
CapabilityBoundingSet=CAP_SYS_ADMIN CAP_DAC_OVERRIDE
```

## API unit contract

The API runs as:

```ini
User=power-service-api
Group=power-service-api
```

and starts:

```text
/usr/local/bin/power-service-api --config /etc/power-service/api.toml
```

Verify installed units match the repository:

```bash
diff -u \
  systemd/power-service-api.service \
  "$(systemctl show power-service-api.service -p FragmentPath --value)"

diff -u \
  systemd/power-service-broker.service \
  "$(systemctl show power-service-broker.service -p FragmentPath --value)"
```

Verify there are no unexpected drop-ins:

```bash
systemctl show power-service-api.service \
  -p FragmentPath \
  -p DropInPaths

systemctl show power-service-broker.service \
  -p FragmentPath \
  -p DropInPaths
```

[Back to top](#top)

---

# 13. Enable and start power-service

If replacing an existing deployment:

```bash
sudo systemctl stop \
  power-service-api \
  power-service-broker
```

Remove stale runtime state if required:

```bash
sudo rm -rf /run/power-service
```

Enable the services:

```bash
sudo systemctl enable \
  power-service-broker.service \
  power-service-api.service
```

Start the broker first:

```bash
sudo systemctl start power-service-broker
```

Then start the API:

```bash
sudo systemctl start power-service-api
```

The API unit requires the broker, so normal boot ordering is handled by systemd.

[Back to top](#top)

---

# 14. Verify the service installation

## Service state

```bash
sudo systemctl status \
  power-service-broker \
  power-service-api \
  --no-pager -l
```

Both should be:

```text
active (running)
```

## Recent logs

```bash
sudo journalctl \
  -u power-service-broker.service \
  -u power-service-api.service \
  --since '5 minutes ago' \
  --no-pager \
  -o short-iso-precise
```

## Runtime directory and Unix socket

```bash
sudo stat -c '%U:%G %a %n' \
  /run/power-service \
  /run/power-service/broker.sock
```

Expected runtime directory:

```text
/run/power-service
0755
```

Expected socket:

```text
/run/power-service/broker.sock
power-service-broker:power-service-api
0660
```

## Listeners

Check HTTPS:

```bash
sudo ss -ltnp | grep 9443
```

Check broker Unix socket:

```bash
sudo ss -lxnp | grep power-service
```

The broker must not expose a TCP listener.

## Verify the live certificate

```bash
openssl s_client \
  -connect <SERVICE_HOSTNAME>:9443 \
  -servername <SERVICE_HOSTNAME> \
  </dev/null 2>/dev/null | \
openssl x509 \
  -noout \
  -subject \
  -issuer \
  -dates \
  -ext subjectAltName
```

## Verify API without credentials

Once the issuing CA is trusted:

```bash
curl \
  https://<SERVICE_HOSTNAME>:9443/v1/status
```

Expected application result:

```text
401 unauthenticated
```

A `401` here is useful: TLS and hostname verification succeeded and the request reached the application.

## Verify authenticated status

Read a status credential without exposing it in shell history:

```bash
read -s STATUS_BEARER
echo
export STATUS_BEARER
```

Then:

```bash
curl \
  -H "Authorization: Bearer $STATUS_BEARER" \
  https://<SERVICE_HOSTNAME>:9443/v1/status
```

Expected:

```text
HTTP 200
```

Clean up:

```bash
unset STATUS_BEARER
```

[Back to top](#top)

---

# 15. Install power-client

The client does not require `cpupower` or `nvidia-smi`.

Required:

```text
Python 3.11 or later
Python venv support
network access to power-service
trusted TLS CA
```

Clone or enter the repository:

```bash
cd power-control/power-client
```

Create a virtual environment:

```bash
python3 -m venv .venv
```

Activate on Linux:

```bash
source .venv/bin/activate
```

Activate on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install:

```bash
pip install .
```

This installs:

```text
powerctl
```

On Windows the executable is:

```text
powerctl.exe
```

Verify:

```bash
powerctl --help
```

[Back to top](#top)

---

# 16. Install client credentials

The client reads bearer credentials from a protected file.

Recommended Linux layout:

```text
~/.config/power-client/
├── config.toml
└── credentials/
    ├── power-service-status
    └── power-service-control
```

Create it:

```bash
mkdir -p \
  ~/.config/power-client/credentials
```

Temporarily disable shell history before entering bearer values:

```bash
set +o history
```

Write the credentials:

```bash
printf '%s\n' '<POWER-SERVICE-STATUS-BEARER>' \
  > ~/.config/power-client/credentials/power-service-status

printf '%s\n' '<POWER-SERVICE-CONTROL-BEARER>' \
  > ~/.config/power-client/credentials/power-service-control
```

Protect them:

```bash
chmod 600 \
  ~/.config/power-client/credentials/power-service-status \
  ~/.config/power-client/credentials/power-service-control
```

Re-enable shell history:

```bash
set -o history
```

The file contains exactly one bearer value:

```text
credential-id.secret
```

The client deliberately rejects insecure credential-file permissions.

On POSIX, the file must not be accessible by group or other users.

On Windows, the credential ACL must be restricted to the owner and permitted system/administrator principals.

[Back to top](#top)

---

# 17. Configure power-client

The default user configuration path is:

```text
~/.config/power-client/config.toml
```

Configuration precedence is:

```text
--config
POWERCTL_CONFIG
$XDG_CONFIG_HOME/power-client/config.toml
~/.config/power-client/config.toml
```

There are no built-in deployment endpoint or credential defaults.

## Linux example

```toml
[service]
endpoint = "https://<SERVICE_HOSTNAME>:9443"
request_timeout = 5.0
credential_file = "/home/<USER>/.config/power-client/credentials/power-service-control"
# additional_ca_bundle_path = "/absolute/path/to/public-ca.pem"

[wake]
mac_address = "<SERVER_MAC_ADDRESS>"
broadcast_address = "<NETWORK_BROADCAST_ADDRESS>"
udp_port = 9
service_wait_timeout = 60.0
service_wait_poll_interval = 2.0
```

## Windows example

```toml
[service]
endpoint = "https://<SERVICE_HOSTNAME>:9443"
request_timeout = 5.0
credential_file = "C:\\Users\\<USER>\\.config\\power-client\\credentials\\power-service-control.txt"
# additional_ca_bundle_path = "C:\\absolute\\path\\to\\public-ca.pem"

[wake]
mac_address = "<SERVER_MAC_ADDRESS>"
broadcast_address = "<NETWORK_BROADCAST_ADDRESS>"
udp_port = 9
service_wait_timeout = 60.0
service_wait_poll_interval = 2.0
```

Use the service hostname represented by the TLS certificate whenever practical.

## Optional workload-readiness probes

Example:

```toml
[wake]
mac_address = "<SERVER_MAC_ADDRESS>"
broadcast_address = "<NETWORK_BROADCAST_ADDRESS>"
udp_port = 9
service_wait_timeout = 60.0
service_wait_poll_interval = 2.0

readiness_wait_timeout = 120.0
readiness_attempt_timeout = 5.0
readiness_wait_poll_interval = 2.0

[[wake.probe]]
identity = "workload-http"
type = "http"
url = "https://workload.example.test/health"
expected_statuses = [200]

[[wake.probe]]
identity = "workload-tcp"
type = "tcp"
host = "<WORKLOAD_HOST>"
port = <WORKLOAD_PORT>
```

Readiness probes are optional deployment configuration.

Do not use demonstration/test probes as production readiness policy.

[Back to top](#top)

---

# 18. Configure client CA trust

The preferred deployment model is operating-system trust.

Install the public root certificate of the CA that issued the power-service server certificate.

The CA private key is never installed on clients.

## Ubuntu/Debian

Install the public CA root:

```bash
sudo install \
  -o root \
  -g root \
  -m 0644 \
  <PUBLIC_CA_ROOT>.crt \
  /usr/local/share/ca-certificates/<CA_NAME>.crt
```

Refresh trust:

```bash
sudo update-ca-certificates
```

Verify without an explicit CA path:

```bash
curl \
  https://<SERVICE_HOSTNAME>:9443/v1/status
```

An application `401 unauthenticated` response confirms the HTTPS connection was trusted and reached the service.

## Windows

Install the public issuing CA certificate into the appropriate trusted root certificate store for the user or machine running `powerctl`.

After OS trust is established, leave:

```toml
# additional_ca_bundle_path = "..."
```

unset/commented.

## Explicit CA bundle alternative

For isolated systems where the OS trust store should not be modified:

```toml
additional_ca_bundle_path = "/absolute/path/to/public-ca.pem"
```

This is an additional trust bundle; TLS certificate and hostname validation remain enabled.

[Back to top](#top)

---

# 19. Verify power-client

## Status

```bash
powerctl status
```

For machine-readable output:

```bash
powerctl --output json status
```

## Profiles

List:

```bash
powerctl profile list
```

Apply:

```bash
powerctl profile apply balanced
```

## Leases

List active leases:

```bash
powerctl lease list
```

Acquire:

```bash
powerctl lease acquire 300
```

The duration is in seconds when no suffix is supplied. The CLI also supports
`h` for hours, `m` for minutes, and `s` for seconds. Combined durations must
use units in largest-to-smallest order:

```bash
powerctl lease acquire 8h
powerctl lease acquire 30m
powerctl lease acquire 3h30m
powerctl lease acquire 1h15m30s
```

Renew:

```bash
powerctl lease renew <LEASE_ID> 300
```

Renewal accepts the same duration forms, for example:

```bash
powerctl lease renew <LEASE_ID> 45m
```

Release:

```bash
powerctl lease release <LEASE_ID>
```

## Wake

```bash
powerctl wake
```

Wake waits for authenticated power-service readiness.

If workload readiness is configured, the configured probes execute after service readiness.

An ad-hoc target can also be supplied:

```bash
powerctl wake tcp://<HOST>:<PORT>
```

or:

```bash
powerctl wake https://<HOST>/health
```

## Suspend

Only after installation and wake access have been verified:

```bash
powerctl suspend
```

Confirm the host suspends and can subsequently be restored through Wake-on-LAN.

[Back to top](#top)

---

# 20. Updating power-service

From the current accepted repository revision:

```bash
cd power-control/power-service
```

Stop the services:

```bash
sudo systemctl stop \
  power-service-api \
  power-service-broker
```

Reinstall the Python package:

```bash
sudo /opt/power-service/bin/pip install .
```

If systemd assets changed, reinstall them:

```bash
sudo install \
  -o root \
  -g root \
  -m 0644 \
  systemd/power-service-api.service \
  /etc/systemd/system/power-service-api.service

sudo install \
  -o root \
  -g root \
  -m 0644 \
  systemd/power-service-broker.service \
  /etc/systemd/system/power-service-broker.service

sudo install \
  -o root \
  -g root \
  -m 0644 \
  systemd/49-power-service-broker.rules \
  /etc/polkit-1/rules.d/49-power-service-broker.rules

sudo install \
  -o root \
  -g root \
  -m 0755 \
  systemd/power-service-reconcile \
  /usr/lib/systemd/system-sleep/power-service-reconcile
```

Reload systemd if unit files changed:

```bash
sudo systemctl daemon-reload
```

Remove stale runtime state:

```bash
sudo rm -rf /run/power-service
```

Start broker:

```bash
sudo systemctl start power-service-broker
```

Start API:

```bash
sudo systemctl start power-service-api
```

Verify:

```bash
sudo systemctl status \
  power-service-broker \
  power-service-api \
  --no-pager
```

Review recent logs:

```bash
sudo journalctl \
  -u power-service-broker.service \
  -u power-service-api.service \
  --since '5 minutes ago' \
  --no-pager \
  -o short-iso-precise
```

[Back to top](#top)

---

# 21. Updating power-client

Activate the client's virtual environment.

Linux:

```bash
cd power-control/power-client
source .venv/bin/activate
```

Windows PowerShell:

```powershell
cd power-control\power-client
.\.venv\Scripts\Activate.ps1
```

Reinstall:

```bash
pip install .
```

Existing user configuration and credential files remain external to the package installation.

Verify:

```bash
powerctl status
```

[Back to top](#top)

---

# 22. Configuration-only changes

## Broker configuration

Edit:

```bash
sudoedit /etc/power-service/broker.toml
```

Then restart the broker:

```bash
sudo systemctl restart power-service-broker
```

Verify:

```bash
sudo systemctl status \
  power-service-broker \
  --no-pager

sudo journalctl \
  -u power-service-broker \
  -n 100 \
  --no-pager
```

A Python-package reinstall is not required for configuration-only changes.

## API configuration

Edit:

```bash
sudoedit /etc/power-service/api.toml
```

Restart:

```bash
sudo systemctl restart power-service-api
```

## Client configuration

Edit:

```text
~/.config/power-client/config.toml
```

No service restart or client reinstall is required.

[Back to top](#top)

---

# 23. Installed filesystem layout

A normal service installation should resemble:

```text
/opt/power-service/
└── Python virtual environment + installed package

/usr/local/bin/
├── power-service-api -> /opt/power-service/bin/power-service-api
└── power-service-broker -> /opt/power-service/bin/power-service-broker

/etc/power-service/
├── api.toml
├── broker.toml
└── tls/
    ├── server.crt
    └── server.key

/etc/systemd/system/
├── power-service-api.service
└── power-service-broker.service

/etc/polkit-1/rules.d/
└── 49-power-service-broker.rules

/usr/lib/systemd/system-sleep/
└── power-service-reconcile

/run/power-service/
└── broker.sock
```

The runtime directory disappears across reboot and is recreated by systemd.

A normal Linux client installation keeps deployment state under the user's home directory:

```text
~/.config/power-client/
├── config.toml
└── credentials/
    ├── power-service-status
    └── power-service-control
```

[Back to top](#top)

---

# 24. Machine-specific example configuration

The following illustrates one deployed host. These values are examples, not portable defaults.

## Example API

```toml
listen_host = "192.168.20.5"
listen_port = 9443
broker_socket = "/run/power-service/broker.sock"
tls_cert = "/etc/power-service/tls/server.crt"
tls_key = "/etc/power-service/tls/server.key"
request_timeout_seconds = 5

[[credentials]]
id = "power-service-status"
salt_hex = "<SALT>"
scrypt_hash_hex = "<HASH>"
permissions = ["status"]

[[credentials]]
id = "power-service-control"
salt_hex = "<SALT>"
scrypt_hash_hex = "<HASH>"
permissions = ["status", "profile", "suspend", "lease"]
```

## Example broker lifecycle policy

```toml
socket_path = "/run/power-service/broker.sock"
api_uid = <POWER_SERVICE_API_UID>
gpu_index = 0
query_timeout_seconds = 5
operation_timeout_seconds = 5

[automatic_suspend]
enabled = true
interactive_sessions_enabled = true
interactive_activity_timeout_seconds = 20
max_lease_ttl_seconds = 28800
stable_idle_seconds = 3600
grace_seconds = 180
evaluation_interval_seconds = 10
max_status_principals = 20
local_activity_probes = []
```

## Example profiles

```toml
[[profiles]]
name = "max"

[profiles.cpu]
policy_ids = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
governor = "performance"
min_khz = 3401000
max_khz = 3401000
epp = "performance"

[profiles.gpu]
power_limit_w = "350"

[[profiles]]
name = "eps"

[profiles.cpu]
policy_ids = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
governor = "powersave"
min_khz = 575976
max_khz = 575976
epp = "power"

[profiles.gpu]
power_limit_w = "100"

[[profiles]]
name = "ps"

[profiles.cpu]
policy_ids = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
governor = "powersave"
min_khz = 575976
max_khz = 1755355
epp = "power"

[profiles.gpu]
power_limit_w = "175"

[[profiles]]
name = "balanced"

[profiles.cpu]
policy_ids = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
governor = "powersave"
min_khz = 1755355
max_khz = 3401000
epp = "balance_performance"

[profiles.gpu]
power_limit_w = "350"

[[profiles]]
name = "balanced-quiet"

[profiles.cpu]
policy_ids = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
governor = "powersave"
min_khz = 1755355
max_khz = 3401000
epp = "balance_performance"

[profiles.gpu]
power_limit_w = "250"
```

These CPU policy IDs, frequency limits, EPP values, GPU limits, idle periods, lease limits, and network values must be selected for the target machine rather than treated as application defaults.

[Back to top](#top)
