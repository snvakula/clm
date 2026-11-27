# Certificate Lifecycle Management (CLM)

CLM is a planned self-hosted system for centralised issuance, renewal, private key rotation and secure distribution of publicly trusted TLS certificates.

## Architecture

- `clm`: local administrative CLI.
- `clmd`: authenticated HTTPS distribution API.
- `clm-client`: pull-based installation and automatic certificate updates, with service validation and rollback.

The target platform is Debian 13, using Python, SQLite, FastAPI, Nginx, systemd and acme.sh. Certificate issuance uses Let's Encrypt production and DNS-01 through AWS Route 53, with isolated ACME instances per AWS account.

CLM supports individual and wildcard certificates, certificate reuse across authorised clients and atomic versioned certificate storage. Deployment settings are organisation-independent.

## Project status

Specification defined; implementation not yet available.

See [technical specification](docs/specification.md) for requirements and acceptance criteria, and [development instructions](AGENTS.md) for coding agent guidance.
