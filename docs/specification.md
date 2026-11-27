# Certificate Lifecycle Management (CLM)

## 1. Project objective

Develop a production-ready, self-hosted Certificate Lifecycle Management system for general-purpose infrastructure.

The system must provide:

- Centralised issuance of publicly trusted TLS certificates.
- Automatic certificate renewal and private key rotation.
- Centralised certificate and private key storage.
- Secure certificate and private key distribution.
- Certificate reuse across multiple authorised clients.
- Simple client-side installation and automatic updates.
- Service configuration validation, graceful reload and automatic rollback.
- Centralised access management and auditing.

The system must support migration from existing certificate distribution services and individual update scripts.

## 2. General requirements

| Parameter | Requirement |
|---|---|
| Project name | CLM |
| Server hostname | Operator-configured FQDN (e.g. clm.example.com) |
| Server installation directory | /opt/clm |
| Client installation directory | /opt/clm-client |
| Programming language | Python 3 |
| Primary operating system | Debian 13 |
| ACME engine | acme.sh |
| Certificate authority | Let's Encrypt |
| ACME challenge | DNS-01 only |
| DNS provider | AWS Route 53 only |
| Multiple AWS accounts | Required |
| DNS challenge delegation | CNAME |
| Internal CA | Not required |
| Multiple SANs | Not supported |
| Database | SQLite |
| REST API | FastAPI |
| Reverse proxy | Nginx |
| Authentication | Individual bearer tokens |
| Scheduler | systemd |
| Containerisation | Not permitted |

Use British English throughout the source code comments, documentation, CLI messages and application logs.

Prefer simplicity, reliability, maintainability and minimal dependencies over unnecessary abstraction.

Do not implement an ACME protocol client, custom certificate authority or proprietary cryptographic algorithms.

## 3. Architecture

The system consists of three logical components:

- `clm` — administrative command-line interface.
- `clmd` — certificate distribution API service.
- `clm-client` — client-side certificate installation and update utility.

The administrative CLI and API service share the same server-side database and certificate repository.

Certificate issuance and renewal must be performed by acme.sh, invoked and managed by the Python application.

The API service must not expose administrative certificate issuance or client management operations.

All administrative operations must be performed locally on the CLM server.

The distribution model must be pull-based. The CLM server must not initiate connections to client systems.

## 4. Certificate identity and reuse

Each certificate must represent exactly one DNS identifier.

The following certificates are independent objects:

- example.com
- *.example.com
- git.example.com
- *.site1.example.com
- *.example.net

A wildcard certificate must never be treated as a substitute for an individual hostname certificate.

Issuing `*.example.com` must not automatically include `example.com`.

Multiple SANs are outside the project scope.

Certificate identifiers must be normalised to lowercase DNS names. Internationalised domain names must be converted to their canonical ASCII representation.

Wildcard identifiers must only be accepted in the form of a single leftmost wildcard label.

The relationship between certificates and clients must be many-to-many.

A certificate may be assigned to multiple clients. A client may have access to multiple certificates.

When an administrator grants a client access to a certificate:

1. Validate and normalise the requested certificate identifier.
2. Check whether the certificate already exists.
3. Reuse the existing certificate and private key if available and valid.
4. Issue a new certificate if it does not exist.
5. Renew or repair an existing certificate if it is no longer usable.
6. Grant access only after a valid certificate is available.

Do not issue duplicate certificates for different clients.

The certificate identifier must be unique within the repository.

Changing the key algorithm or other certificate properties must be an explicit administrative operation, not an implicit reason to create a duplicate certificate.

## 5. Server directory structure

All application-specific files must be stored under `/opt/clm`.

```text
/opt/clm/
├── bin/
│   ├── clm
│   └── clmd
├── app/
├── venv/
├── acme/
│   ├── engine/
│   └── instances/
├── config/
│   ├── clm.yaml
│   ├── route53.yaml
│   └── aws/
│       ├── config
│       └── credentials
├── data/
│   ├── clm.db
│   └── certificates/
├── logs/
└── run/
```

System-level integration files may be installed outside this directory where required:

- systemd units under `/etc/systemd/system/`.
- Nginx configuration under `/etc/nginx/`.
- CLI symbolic links under `/usr/local/bin/`.
- Operating system packages and system users.

Do not create application-specific storage directories under `/var/lib`, `/var/log` or `/etc`.

## 6. ACME integration

Use acme.sh as an external ACME engine.

Requirements:

- Install acme.sh under `/opt/clm/acme/`.
- Explicitly configure Let's Encrypt as the certificate authority.
- Use the built-in `dns_aws` DNS plugin.
- Use DNS-01 for all certificates, including non-wildcard certificates.
- Support the built-in challenge alias mode.
- Disable the native acme.sh cron job.
- Manage all renewal operations through CLM.
- Use the supported `--install-cert` interface to export certificate material.
- Do not depend on undocumented acme.sh internal file formats.
- Do not automatically upgrade acme.sh during normal certificate operations.
- Use the Let's Encrypt production ACME environment exclusively; do not implement alternative ACME environments or related CLI options, configuration or installation procedures.
- Respect Let's Encrypt rate limits.

Invoke acme.sh through Python subprocess APIs using argument arrays.

Do not use `shell=True` or construct executable shell commands from user input.

Capture and process exit codes correctly, including cases where renewal is not yet required.

The default key algorithm must be ECDSA P-256.

Support the following alternative key algorithms:

- ECDSA P-384.
- RSA 2048.
- RSA 3072.
- RSA 4096.

Generate a new private key during each successful certificate renewal.

Provide an explicit administrative operation for immediate private key rotation and certificate reissuance.

Use the renewal eligibility mechanism provided by acme.sh. Do not hard-code assumptions about certificate lifetime or renewal thresholds.

## 7. Route 53 integration

AWS Route 53 is the only supported DNS provider.

Multiple AWS accounts must be supported.

Different certificate domains may delegate ACME validation to different challenge zones managed by different AWS accounts.

Example DNS records:

```dns
_acme-challenge.example.com.     CNAME _acme-challenge.acme.example.com.
_acme-challenge.example.net.  CNAME _acme-challenge.acme.example.com.
_acme-challenge.example.org.     CNAME _acme-challenge.acme.example.org.
```

For individual hostname certificates, the corresponding challenge record must exist separately:

```dns
_acme-challenge.git.example.com. CNAME _acme-challenge.acme.example.com.
```

The application must not assume that a challenge delegation for a parent domain also applies to its subdomains.

### 7.1 AWS account configuration

Separate AWS account definitions from challenge alias mappings. Each account may manage multiple aliases and hosted zones.

Example `/opt/clm/config/route53.yaml`:

```yaml
accounts:
  primary:
    credentials_profile: primary
  secondary:
    credentials_profile: secondary

challenge_aliases:
  acme.example.com:
    account: primary
    hosted_zone_id: ZXXXXXXXXXXXXX
  acme.example.org:
    account: secondary
    hosted_zone_id: ZYYYYYYYYYYYYY
```

Hosted zone IDs shown above are placeholders.

AWS credentials must be stored in:

```text
/opt/clm/config/aws/credentials
```

Example:

```ini
[primary]
aws_access_key_id = ...
aws_secret_access_key = ...

[secondary]
aws_access_key_id = ...
aws_secret_access_key = ...
```

Support AWS session tokens where applicable.

Never store AWS credentials in the source code, SQLite database, certificate manifests or logs.

### 7.2 Automatic AWS account selection

For each certificate:

1. Remove the wildcard prefix when constructing the challenge DNS name.
2. Construct the corresponding `_acme-challenge` DNS name.
3. Resolve its public DNS CNAME chain.
4. Identify the configured challenge alias.
5. Determine the AWS account and select its isolated acme.sh instance.
6. Validate the selected challenge endpoint and hosted zone and load the appropriate AWS credentials.
7. Invoke acme.sh within the selected instance using the selected credentials and challenge alias.
8. Validate and publish the resulting certificate material.

Do not use the original certificate domain to determine the AWS account.

Do not require a separate configuration entry for every certificate domain.

Reject unknown or ambiguous challenge aliases.

Reject CNAME loops and excessively long CNAME chains.

Use public DNS resolution for ACME challenge discovery to avoid interference from internal split-horizon DNS.

The application must not automatically modify CNAME records in the original certificate domain.

If the required delegation does not exist, return an actionable error containing the expected challenge record name.

### 7.3 AWS account isolation

Each configured AWS account must correspond to exactly one independent acme.sh instance. The executable and supporting scripts may be shared.

```text
/opt/clm/acme/
├── engine/
│   ├── acme.sh
│   └── dnsapi/
└── instances/
    ├── primary/
    │   ├── config/
    │   ├── certificates/
    │   └── logs/
    └── secondary/
        ├── config/
        ├── certificates/
        └── logs/
```

Each instance must have independent configuration, ACME account registration and account key, AWS credentials, certificate storage, renewal configuration, working state and logs.

Every invocation must explicitly select the appropriate instance directories and credentials using documented acme.sh options. Construct a dedicated subprocess environment for each invocation; never modify the global process environment to switch accounts. Do not inherit unrelated AWS credentials or share account keys, certificate state or renewal configuration. Do not install or enable native acme.sh cron jobs. CLM controls issuance, renewal, revocation and key rotation.

Verify that dns_aws selects the intended hosted zone and does not modify unrelated zones. Do not assume undocumented environment variables override zone selection.

Resolve the public challenge delegation again before renewal and other issuance operations. If the alias or AWS account changes, perform a controlled migration using the correct instance and credentials. Validate and atomically publish the new generation before committing the new account association. Preserve the previous working generation and recoverable account state on failure. Reconcile interrupted migrations; do not silently continue using the previous account or automatically revoke a working certificate during migration.

### 7.4 Concurrency and DNS cleanup

Only one certificate issuance, renewal, revocation, migration or key rotation operation may execute at a time across the entire CLM server.

Use an inter-process lock.

This requirement is particularly important because multiple certificate domains may delegate validation to the same TXT record.

Preserve unrelated TXT values.

Never delete an entire TXT RRset containing values not created by the current operation.

Verify the behaviour of the acme.sh dns_aws plugin during issuance, renewal and cleanup.

If the plugin cannot safely satisfy these requirements, stop and report the limitation before introducing an alternative implementation.

Do not automatically delete unrelated or pre-existing DNS records.

## 8. Certificate repository

Store certificate material separately from the internal acme.sh state.

Use immutable, versioned certificate generations.

Example:

```text
/opt/clm/data/certificates/
└── <certificate-storage-id>/
    ├── current -> versions/000002
    └── versions/
        ├── 000001/
        │   ├── cert.pem
        │   ├── chain.pem
        │   ├── fullchain.pem
        │   ├── privkey.pem
        │   └── manifest.json
        └── 000002/
            ├── cert.pem
            ├── chain.pem
            ├── fullchain.pem
            ├── privkey.pem
            └── manifest.json
```

The storage identifier must be deterministic, filesystem-safe and collision-free.

Do not use literal wildcard characters in storage directory names.

Do not map `*.example.com` to `wildcard.example.com` without a collision prevention mechanism.

### 8.1 Certificate files

- `cert.pem`: leaf certificate.
- `chain.pem`: intermediate certificates.
- `fullchain.pem`: leaf certificate followed by intermediate certificates.
- `privkey.pem`: corresponding private key.
- `manifest.json`: certificate metadata and file checksums.

### 8.2 Manifest

Example:

```json
{
  "schema_version": 1,
  "certificate_id": "*.example.com",
  "version": 2,
  "serial": "03A8F7C219B4",
  "issuer": "Example CA",
  "not_before": "2026-11-27T00:00:00Z",
  "not_after": "2027-02-25T00:00:00Z",
  "key_algorithm": "ECDSA",
  "key_size": 256,
  "files": {
    "cert.pem": {
      "sha256": "..."
    },
    "chain.pem": {
      "sha256": "..."
    },
    "fullchain.pem": {
      "sha256": "..."
    },
    "privkey.pem": {
      "sha256": "..."
    }
  }
}
```

All timestamps must use UTC and ISO 8601.

Certificate metadata must be derived from the actual certificate.

Do not publish a new version if the certificate material has not changed.

### 8.3 Publication

Before publishing a certificate generation:

- Validate all PEM files.
- Verify the exact requested DNS SAN.
- Verify that no additional DNS SANs are present.
- Verify the certificate validity period.
- Verify the certificate chain against trusted CA certificates.
- Verify that the private key matches the leaf certificate.
- Verify that fullchain.pem contains the correct leaf and intermediate certificates.
- Generate SHA-256 checksums.
- Apply appropriate filesystem permissions.

Publish the new generation atomically.

The currently published generation must remain available if issuance, validation or publication fails.

Retain two published generations by default: current and previous.

Remove obsolete generations only after successful publication and database reconciliation.

Protect active downloads from concurrent generation removal.

## 9. Database

Use SQLite.

Enable foreign key enforcement, appropriate transaction handling, a busy timeout and WAL mode where supported.

Maintain schema migrations.

The database must contain at least the following logical entities:

### Certificates

- Internal identifier.
- Canonical DNS identifier.
- Key algorithm.
- AWS account.
- Challenge alias.
- Current generation.
- Certificate serial.
- Certificate validity dates.
- Certificate status.
- Creation timestamp.
- Last successful renewal timestamp.
- Last renewal attempt.
- Last error.

### Clients

- Internal identifier.
- Unique client name.
- Token hash.
- Enabled or disabled status.
- Creation timestamp.
- Last successful authentication.
- Last successful certificate download.

### Certificate access

- Client identifier.
- Certificate identifier.
- Grant timestamp.

Enforce uniqueness of each client-to-certificate relationship.

### Audit events

- Timestamp.
- Operation.
- Client or administrator identity.
- Certificate identifier.
- Generation.
- Result.
- Source address, where applicable.
- Sanitised error details.

Do not store raw authentication tokens, AWS credentials or private key contents in the database.

Implement reconciliation between the database and filesystem repository following interrupted operations.

## 10. Administrative CLI

The CLI must provide consistent help, meaningful exit codes and human-readable output.

Support machine-readable JSON output for list, show and status operations.

### 10.1 Certificate management

```bash
clm cert issue git.example.com
clm cert issue '*.example.com'
clm cert issue '*.example.net' --key rsa-2048
clm cert list
clm cert show '*.example.com'
clm cert renew '*.example.com'
clm cert renew --all
clm cert rotate-key '*.example.com'
clm cert revoke '*.example.com'
clm cert delete '*.example.com'
clm cert import
```

Issuance must be idempotent.

Renewal must not force reissuance unless explicitly requested.

Key rotation must force generation of a new private key and corresponding certificate.

Revocation must not silently delete the certificate from the repository.

Deletion must be refused if active client assignments exist, unless explicitly confirmed through a documented force operation.

Import must support migration of existing certificate material into CLM after full validation.

Imported certificates must have a verified renewal configuration before being marked as automatically renewable.

### 10.2 Client management

```bash
clm client add git
clm client list
clm client show git
clm client grant git git.example.com
clm client grant git '*.example.net'
clm client deny git '*.example.net'
clm client rotate-token git
clm client disable git
clm client enable git
clm client delete git
```

Client names must support letters, digits, hyphens and dots.

Examples:

```text
git
jira
kb
apricot
cherry
k8s-cp01.site1
vpn-site2-01
```

Client creation must generate a cryptographically secure access token.

Display the plaintext token only when created or rotated.

Token rotation must invalidate the previous token.

Disabling a client must immediately prevent further API access.

Granting access must reuse an existing valid certificate where possible.

Denying access must immediately prevent further downloads of the affected certificate.

### 10.3 System management

```bash
clm status
clm check
clm version
```

The system check must verify configuration, database integrity, repository consistency, acme.sh availability and required filesystem permissions.

## 11. Distribution API

Use a versioned HTTPS REST API.

Base URL:

```text
https://clm.example.com/v1
```

Authentication:

```http
Authorization: Bearer <client-token>
```

### 11.1 Endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | /v1/health | Minimal health status |
| GET | /v1/client | Authenticated client information |
| GET | /v1/certificates | List authorised certificates |
| GET | /v1/certificates/{id}/manifest | Current certificate manifest |
| GET | /v1/certificates/{id}/versions/{version}/bundle | Download a specific certificate generation |

Use a URL-safe certificate identifier.

Do not place raw wildcard identifiers into filesystem paths.

The certificate list must contain only certificates assigned to the authenticated client.

Every certificate download request must independently verify authentication, client status and certificate access.

The bundle must contain the complete certificate generation, including the private key.

Use a documented archive format containing only the expected certificate files and manifest.

Do not permit arbitrary filesystem access.

Do not expose acme.sh account files, AWS credentials, database files or historical private keys through directory listing.

Historical generations must not be available to clients unless required to complete an authorised in-progress update.

Support conditional requests or lightweight manifest checks to avoid unnecessary downloads.

Return appropriate HTTP status codes.

Disable caching of sensitive API responses:

```http
Cache-Control: no-store
```

Do not include credentials or sensitive internal details in error responses.

## 12. Authentication and security

Generate client tokens using a cryptographically secure random number generator with at least 256 bits of entropy.

Store only token hashes on the server.

Use constant-time comparison where applicable.

Protect the API against excessive authentication attempts.

Do not accept credentials through URL query parameters.

Never log bearer tokens.

The CLM API must only be accessible from operator-authorised networks and VPN clients. The permitted networks and server hostname must be deployment-specific configuration, never hard-coded.

Use Nginx as the HTTPS reverse proxy.

Allow TLS 1.2 and TLS 1.3 only.

Do not disable TLS certificate verification on clients.

Do not hard-code network interface IP addresses into Nginx or application listener configuration.

Nginx must use generic IPv4 and IPv6 listener bindings. Network access restrictions must be enforced through firewall and access control rules.

The application backend must not be directly exposed to untrusted networks. Prefer a Unix domain socket between Nginx and the API service.

Disable directory listing.

Restrict the API to required HTTP methods.

Run the API service using a dedicated unprivileged system account.

Separate API read access from certificate issuance permissions and AWS credential access.

Protect all private keys, authentication credentials and sensitive configuration files using restrictive permissions.

Private key files must use mode 0600 by default.

Store certificates and private keys as standard PEM files without application-level encryption. This applies to published and historical generations, internal acme.sh storage, ACME account keys and temporary processing files. Do not implement encryption or decryption, master encryption keys, encryption key rotation, custom encrypted containers or additional encryption-related application dependencies.

Require appropriately encrypted underlying storage for the server and backups containing private keys or credentials. Document that encryption at rest does not protect against compromise of a running virtual machine, privileged operating system accounts or the CLM application.

AWS credentials, ACME account keys and client credentials must use mode 0600. Use restrictive directory ownership and permissions and a process umask of 0077. Temporary private key files must be securely permissioned from creation. Remove obsolete generations and unnecessary temporary files. Never expose private key storage directly through Nginx; distribute private keys exclusively through the authenticated and authorised HTTPS API. Where non-root service access is required, use narrowly scoped ownership or group permissions through local service configuration.

Document protection of acme.sh working files and backups.

Revoking client access does not invalidate private keys previously downloaded by that client. Document the required certificate and key rotation procedure following client compromise.

## 13. Client directory structure

All application-specific client files must be stored under `/opt/clm-client`.

```text
/opt/clm-client/
├── bin/
│   └── clm-client
├── app/
├── venv/
├── config/
│   ├── client.yaml
│   ├── credentials
│   └── services.d/
├── certificates/
├── data/
├── logs/
└── run/
```

Client credentials must be readable only by the authorised local account.

The client must not require AWS credentials, acme.sh or access to the DNS provider.

## 14. Client CLI

### 14.1 Initialisation

```bash
clm-client init https://clm.example.com
```

Request the access token interactively without echoing it.

Validate the token against the server before saving the configuration.

Support secure non-interactive provisioning through a protected credentials file or standard input.

Do not require tokens to be passed as command-line arguments.

### 14.2 Certificate management

```bash
clm-client available
clm-client install git.example.com
clm-client install '*.example.net'
clm-client install '*.example.net' --service nginx
clm-client list
clm-client show '*.example.net'
clm-client update
clm-client update '*.example.net'
clm-client remove '*.example.net'
clm-client status
clm-client version
```

Installation must:

1. Verify that the client is authorised to obtain the certificate.
2. Download the current certificate generation.
3. Validate all certificate material.
4. Install the generation atomically.
5. Register the certificate for future automatic updates.
6. Associate the certificate with the requested local service profile, if specified.

Installing an already installed certificate must be idempotent.

Installing a certificate must not automatically rewrite the application's configuration files.

Display the resulting certificate and private key paths for the administrator.

Removing a certificate must not silently break an associated service. Require explicit confirmation where appropriate.

## 15. Client certificate storage

Use the same versioned generation model as the server.

```text
/opt/clm-client/certificates/
└── <certificate-storage-id>/
    ├── current -> versions/000002
    └── versions/
        ├── 000001/
        └── 000002/
```

The current generation must expose stable paths for service configuration.

Example Nginx configuration:

```nginx
ssl_certificate /opt/clm-client/certificates/<certificate-storage-id>/current/fullchain.pem;
ssl_certificate_key /opt/clm-client/certificates/<certificate-storage-id>/current/privkey.pem;
```

The client must report the actual storage paths rather than requiring the administrator to calculate them.

Support multiple installed certificates per client.

Support multiple local services using the same certificate.

Retain the current and previous generations by default.

## 16. Client update procedure

For each installed certificate:

1. Retrieve the current remote manifest.
2. Compare the remote generation and checksums with the local state.
3. Skip downloading unchanged certificate material.
4. Download the required immutable generation into a protected staging directory.
5. Validate archive contents and reject unexpected files.
6. Verify SHA-256 checksums.
7. Verify certificate identity, validity and trust chain.
8. Verify that the private key matches the certificate.
9. Apply ownership and permissions.
10. Atomically switch the current generation.
11. Execute local service configuration checks.
12. Gracefully reload affected services.
13. Verify successful activation where supported.
14. Commit the new generation.
15. Remove obsolete generations and temporary files.

Certificate and private key updates must be treated as one transaction.

Do not overwrite live certificate and private key files independently.

Protect updates with an inter-process lock.

Do not reload services if certificate material has not changed.

Where multiple certificates affect the same service, consolidate updates and reload the service once where practical.

If any validation, activation or reload operation fails:

- Restore the previous generation.
- Revalidate the previous configuration.
- Reload the affected service where required.
- Report the failure.
- Return a non-zero exit code.

If rollback fails, preserve the necessary recovery files and clearly report their locations.

An unavailable CLM server must not cause removal or replacement of existing local certificates.

An authentication or authorisation failure must not automatically delete previously installed certificate material.

## 17. Local service profiles

Service profiles must be stored locally under:

```text
/opt/clm-client/config/services.d/
```

Example `nginx.yaml`:

```yaml
name: nginx

check:
  - nginx
  - -t

reload:
  - systemctl
  - reload
  - nginx
```

Example `postfix.yaml`:

```yaml
name: postfix

check:
  - postfix
  - check

reload:
  - systemctl
  - reload
  - postfix
```

Requirements:

- Support custom local service profiles.
- Execute commands using argument arrays.
- Apply execution timeouts.
- Check command exit codes.
- Prefer graceful reload over restart.
- Do not reload services unnecessarily.
- Do not execute arbitrary commands received from the CLM server.
- Protect service profile configuration from modification by unprivileged users.

Initially provide a fully supported Nginx profile.

Additional service integrations must not be advertised as supported until tested.

## 18. Automatic renewal and updates

Use systemd services and timers.

Do not use cron.

### 18.1 Server renewal timer

File: `clm-renew.timer`

```ini
[Unit]
Description=Daily CLM certificate renewal check

[Timer]
OnCalendar=*-*-* 02:00:00 UTC
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
```

The corresponding oneshot service must execute:

```bash
/opt/clm/bin/clm cert renew --all
```

Process certificates sequentially.

A failure to renew one certificate must not prevent attempts to renew other eligible certificates.

Return an overall failure status if any required renewal operation fails.

### 18.2 Client update timer

File: `clm-client-update.timer`

```ini
[Unit]
Description=Daily CLM client certificate update

[Timer]
OnCalendar=*-*-* 04:00:00 UTC
RandomizedDelaySec=3h
Persistent=true

[Install]
WantedBy=timers.target
```

The corresponding oneshot service must execute:

```bash
/opt/clm-client/bin/clm-client update
```

Clients must check for certificate updates once per day.

The normal scheduling window is 04:00–07:00 UTC.

Missed executions must be handled by the persistent timer mechanism.

Manual updates must remain available at any time.

The schedule must be configurable through systemd without modifying application source code.

## 19. Logging and monitoring

Provide structured and human-readable logs.

Store application-specific log files under:

```text
/opt/clm/logs/
/opt/clm-client/logs/
```

Integrate with the systemd journal.

Implement log rotation and retention.

Log at least:

- Certificate issuance.
- Certificate renewal.
- Private key rotation.
- Certificate revocation.
- Certificate publication.
- Client creation and deletion.
- Access grants and revocations.
- Token rotation.
- Authentication failures.
- Certificate and private key downloads.
- Client installation and updates.
- Service reloads.
- Rollback operations.
- DNS validation failures.

Never log private keys, bearer tokens, AWS credentials or ACME challenge secrets.

Provide machine-readable status output suitable for Zabbix monitoring.

Report certificate expiry, failed renewals, failed client updates and repository inconsistencies.

## 20. Installation and upgrades

Provide native installation scripts:

```text
install-server.sh
install-client.sh
```

Provide documented uninstall procedures.

Installation scripts must:

- Support Debian 13.
- Install required operating system dependencies.
- Create required system users and groups.
- Create application directories with appropriate permissions.
- Create Python virtual environments.
- Install pinned Python dependencies.
- Install systemd units.
- Create CLI symbolic links.
- Install or provide the Nginx configuration.
- Validate the resulting configuration.
- Enable the appropriate services and timers.
- Be safe to execute repeatedly.

Do not overwrite existing credentials, configuration or certificate data during repeated installations or upgrades.

Do not enable duplicate timers or cron jobs. Do not automatically issue production certificates during installation, upgrades or automated testing.

Upgrades must preserve existing certificates, keys, clients, access assignments and configuration.

Provide database migration and rollback procedures.

Do not automatically remove application data during uninstallation without explicit confirmation.

Clean up temporary downloads, installation staging directories, test files and other temporary artefacts.

Never leave unnecessary files behind.

## 21. Source code organisation

Use a single repository containing server and client applications.

Suggested layout:

```text
clm/
├── pyproject.toml
├── README.md
├── LICENSE
├── src/
│   ├── clm_common/
│   ├── clm_server/
│   └── clm_client/
├── tests/
│   ├── unit/
│   └── integration/
├── packaging/
│   ├── systemd/
│   └── nginx/
├── scripts/
│   ├── install-server.sh
│   └── install-client.sh
├── examples/
└── docs/
```

Share certificate validation, manifest handling, configuration validation and other genuinely common functionality.

Avoid unnecessary coupling between server and client packages.

The client must not require server-only dependencies.

Use Python type annotations.

Use consistent formatting, linting and automated tests.

Pin production dependencies and document the dependency update process.

## 22. Testing requirements

Provide automated unit and integration tests.

Tests must cover at least:

### Certificate management

- Individual certificate issuance.
- Wildcard certificate issuance.
- Exact certificate identity matching.
- Certificate reuse across clients.
- Rejection of multiple SANs.
- Renewal eligibility.
- Private key rotation.
- Certificate and private key mismatch.
- Invalid certificate chains.
- Expired certificates.
- Interrupted certificate publication.

### DNS and AWS

- CNAME resolution.
- Multiple AWS account selection.
- Unknown challenge alias rejection.
- CNAME loop detection.
- AWS credential isolation.
- Correct hosted zone selection.
- Shared challenge endpoint handling.
- DNS cleanup following failure.
- Concurrent issuance prevention.

### API and authentication

- Valid token authentication.
- Invalid token rejection.
- Disabled client rejection.
- Unauthorised certificate access.
- Token rotation.
- Immediate access revocation.
- Path traversal prevention.
- Prevention of sensitive information leakage.
- Concurrent downloads during certificate publication.

### Client operations

- Initial registration.
- First certificate installation.
- Idempotent installation.
- Unchanged certificate detection.
- Certificate and key updates.
- Invalid manifest rejection.
- Checksum mismatch rejection.
- Malicious archive rejection.
- Atomic generation switching.
- Failed service validation.
- Failed service reload.
- Successful rollback.
- Interrupted update recovery.
- Unavailable CLM server.
- Concurrent update prevention.
- Temporary file cleanup.

### Deployment

- Clean server installation.
- Clean client installation.
- Repeated installation.
- Upgrade without data loss.
- Correct systemd timer configuration.
- File permissions.
- Service account restrictions.

Use mocked AWS and ACME operations for routine automated testing.

Routine automated tests must not contact the production Let's Encrypt API or modify production DNS records. Real issuance tests must be manually and explicitly initiated by an administrator using designated test domains and authorised AWS credentials.

Never use production AWS credentials or production certificates in automated tests.

Additionally test independent instance creation, ACME account key and certificate state isolation, multiple aliases per AWS account, controlled account migration including failure recovery, correct renewal instance selection, global locking, private key permissions and prevention of unauthorised filesystem access.

## 23. Documentation

Provide:

- Architecture overview.
- Server installation guide.
- Client installation guide.
- AWS IAM policy examples.
- Route 53 configuration guide.
- DNS CNAME delegation examples.
- Administrative CLI reference.
- Client CLI reference.
- REST API documentation.
- Nginx integration guide.
- Certificate migration guide.
- Backup and restoration procedures.
- Private key protection and recovery procedures.
- Client compromise response procedure.
- Troubleshooting guide.
- Upgrade and uninstallation procedures.

Use reserved example domain names for illustrative configuration; real issuance requires administrator-controlled domains. Never include actual secrets.

## 24. Acceptance criteria

The project is accepted when the following illustrative workflow operates successfully using administrator-controlled domains in place of the reserved example domains. Real issuance is a manual acceptance test.

On the CLM server:

```bash
clm cert issue '*.example.com'
clm cert issue git.example.com
clm client add git
clm client grant git git.example.com
clm client add jira
clm client grant jira '*.example.com'
clm client add kb
clm client grant kb '*.example.com'
```

On the Git server:

```bash
clm-client init https://clm.example.com
clm-client install git.example.com --service nginx
```

On the Jira and KB servers:

```bash
clm-client init https://clm.example.com
clm-client install '*.example.com' --service nginx
```

The system must demonstrate that:

- git.example.com and *.example.com are independent certificates.
- Jira and KB receive the same wildcard certificate and private key.
- Git cannot download certificates that have not been assigned to it.
- Granting another client access to an existing certificate does not cause duplicate issuance.
- Certificates delegated to different AWS accounts are issued using the correct credentials.
- Certificate renewal generates a new private key.
- Clients receive the renewed certificate and corresponding private key together.
- Nginx reloads successfully without a full restart.
- Failed updates restore the previous working certificate generation.
- Automatic renewal and client updates execute according to the specified schedules.
- Repeated installation does not damage existing data.
- No unnecessary temporary files remain after successful operations.

## 25. Implementation approach

Implement the project incrementally:

1. Common configuration and certificate validation.
2. SQLite database and certificate repository.
3. Route 53 account and alias discovery and acme.sh integration.
4. Administrative CLI.
5. Client authentication and certificate access management.
6. Distribution API.
7. Client CLI and atomic certificate installation.
8. Service profiles and rollback.
9. Installation scripts and systemd integration.
10. Automated testing and documentation.

Before implementing the ACME integration, verify the current documented behaviour of acme.sh, particularly:

- Challenge alias mode.
- dns_aws credential persistence.
- AWS hosted zone selection.
- Separate configuration directories.
- Renewal exit codes.
- Private key rotation.
- Certificate export.
- TXT record cleanup.

Do not invent unsupported acme.sh options or rely on unverified assumptions.

If a requirement conflicts with the actual capabilities of acme.sh or another selected component, report the conflict and propose the smallest viable architectural adjustment.

Do not silently change the agreed architecture.

Deliver working source code, installation scripts, configuration examples, automated tests and complete documentation. Do not deliver pseudocode or placeholder implementations for core functionality.