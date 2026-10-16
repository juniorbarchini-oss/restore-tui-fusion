# Security policy

This tool runs with elevated privileges, handles SSH access and may store a Proxmox Backup Server token
(`PBS_PASSWORD`) in `~/.config/restore-tui/config.conf` (mode 600). Security reports are taken seriously.

## Supported versions

Only the latest release and the `main` branch receive fixes. The project is alpha.

## Reporting a vulnerability

Please **do not** open a public issue with exploit details. Use GitHub's private vulnerability reporting
(the "Report a vulnerability" button under this repository's *Security* tab). If that is not available,
open an issue that says only that you have a security report, without technical details, and a private
channel will be arranged.

Useful information: affected version or commit, what an attacker can do, and steps to reproduce.

## Out of scope

Data loss caused by running the tool with a wrong configuration is a bug worth reporting, but it is not a
security vulnerability unless it can be triggered by an untrusted party.
