# Certificate Lifecycle Management 1.0.0

CLM centralises publicly trusted TLS certificate issuance and renewal on Debian 13. Authorised clients pull versioned PEM certificate and private key bundles over HTTPS, validate them and activate them together. Each certificate covers one exact hostname or wildcard and can be shared by several clients.

- `clm` is the local administrative CLI. It uses SQLite, an immutable certificate repository and one isolated acme.sh instance per AWS account.
- `clmd` is the distribution API. Nginx terminates HTTPS and forwards requests to the API through a Unix socket.
- `clm-client` is the Linux client. It synchronises all assigned certificates and runs local hooks when files change.

CLM uses Let's Encrypt production and Route 53 DNS-01. It uses a pinned acme.sh release with a reviewed `dns_aws` patch that restricts writes to the configured public hosted zone and preserves unrelated TXT values.

## Start here

- [Administrator guide](docs/administration.md) for native installation, AWS, delegation, CLI commands, migration and the distribution API.
- [Linux client guide](docs/client.md) for configuration, update hooks and scheduling.
- [Operations guide](docs/operations.md) for security, monitoring, upgrades, backups, recovery and deployment checks.
- [Technical specification](docs/specification.md), [implementation plan](docs/implementation-plan.md) and [development instructions](AGENTS.md).
