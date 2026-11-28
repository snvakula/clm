# Implementation plan and verification

CLM 1.1.0 implements the [technical specification](specification.md).

| Stage | Implementation |
|---|---|
| 1. Project foundation | Independent application sources, certificate identity, validation and storage interfaces. |
| 2. Server and repository | Transactional SQLite metadata, immutable generations, atomic publication and recovery. |
| 3. ACME and Route 53 | Isolated account state, production Let's Encrypt, public delegation and recoverable DNS changes. |
| 4. Server CLI and API | Local administration and authorised distribution through Nginx and a Unix socket. |
| 5. Clients | Distribution-independent Python client for Linux, with authenticated batch updates, validation and rollback hooks. |
| 6. Installation | Debian server installer, portable Linux client installer and scheduler instructions. |
| 7. Verification | Unit, integration, security, fault-recovery and packaging tests. Production CA and AWS operations are mocked. |
| 8. Documentation | Administrator, client and operational guides, including backup and recovery. |

## Verification

- The automated suite covers authenticated downloads, certificate validation, secret and filesystem permissions, publication and recovery, client hooks, the pinned acme.sh patch and Nginx configuration.
- All 72 tests passed on Debian 13, including HTTP failure diagnostics without exposing tokens or internal error details. On Alpine 3.23, a clean client installation and a repeat installation passed using the hash-locked musl wheels. The installed client validated and published a test certificate downloaded over local HTTPS.
- Ruff, ShellCheck and dependency audits run in CI. Production credentials are excluded from automated testing.
- Deployment checks include repeat installation with a live systemd, local scheduling for clients without systemd, server account isolation under deployment credentials and restoration from a backup.

## Decisions and known limitations

- Approved deviation in §7.4. Upstream acme.sh 3.1.7 `dns_aws_rm` could delete the whole TXT record set. CLM uses the reviewed, pinned plugin patch, preserving other TXT values with a conditional, transactional change and verifying the exact configured public hosted zone. The patch also keeps AWS signing credentials and challenge values out of process arguments. A simple TXT record set is required at the alias endpoint.
- The client ships an optional web server restart hook. The server deployment supplies the TLS bootstrap certificate and site ACLs.
- Google Public DNS over HTTPS is the public resolver. Authorised deployment networks must permit the outbound request.
- Imported certificates are eligible for automatic renewal after their first successful explicit renewal through the selected ACME account. An unresolved CA revocation can require operator confirmation from the issuer.
- Multi-SAN certificates, alternative ACME environments and application-level encryption are outside scope. The deployment requires encrypted underlying storage and backups.
- Full deployment verification requires a Debian 13 server and Linux client hosts with root and a running systemd, operator-controlled DNS with scoped AWS credentials, and real Nginx service reloads. Confirm repeated full installations and upgrades, account isolation under the deployed service identity, timers, issued certificate and key rotation, and a full backup restoration. See the [deployment checks](operations.md#deployment-checks).

## Final review against the specification

| Section | Review |
|---|---|
| 1. Project objective | Issuance, renewal, private key rotation, authenticated distribution, local activation and migration are provided by the server and Linux client. |
| 2. General requirements | Python, Debian 13 server, Linux client, SQLite, FastAPI, Nginx, production Let's Encrypt and Route 53. The implementation uses British English. |
| 3. Architecture | Local administration, distribution through a Unix socket and Nginx, and a pull-only client. |
| 4. Identity and reuse | Canonical IDNA DNS identities, independent wildcard and exact names, unique storage and many-to-many grants. |
| 5. Server directories | Native layout under `/opt/clm`; OS integration files in the applicable system locations. |
| 6. ACME integration | Pinned acme.sh flags verified against source. Renewal eligibility and rotation delegated to the engine with validated export. Live CA deployment check pending. |
| 7. Route 53 | Public CNAME discovery, exact hosted zone, account isolation, global locking, conditional TXT updates and recoverable migration. Live Route 53 deployment check pending. |
| 8. Repository | Validated, immutable PEM generations, authenticated checksums, atomic switching, retention and download leases. |
| 9. Database | SQLite schema version, WAL, busy timeouts, foreign keys, audit trail and publication reconciliation. |
| 10. Administrative CLI | Certificate, client, import, status, check and version commands; JSON inspection and explicit destructive controls. |
| 11. Distribution API | Versioned GET endpoints, scoped authentication, manifests and bundles, sensitive response cache controls. |
| 12. Authentication and security | Hashed random tokens, API identity, rate limits, restricted socket, ACL template, PEM permissions and encrypted-storage guidance. Root deployment checks pending. |
| 13. Client directories | A consistent `/opt/clm-client` layout across Linux, with protected credentials and certificate files. |
| 14. Client CLI | Manual endpoint and token configuration, update of all assignments, status and version. |
| 15. Client storage | Versioned generations with stable paths and current and previous retention. |
| 16. Client updates | Bounded manifest and archive verification, serialised switching, service checks, rollback and interrupted update recovery. |
| 17. Update hooks | Local executable scripts, per-hook timeouts, rollback events and a bundled example for running web servers. |
| 18. Automatic renewal and updates | Persistent systemd timers and scheduler guidance for Linux hosts without systemd. Actual timer execution on deployed hosts pending. |
| 19. Logging and monitoring | Structured logs and journal output, rotation, SQLite audit and machine-readable expiry and failure status. |
| 20. Installation and upgrades | Native installers with dependency checks and state preservation. Full privileged installation, repetition and upgrade pending. |
| 21. Source layout | Independent `src/server/` and `src/client_linux/` packages. |
| 22. Testing | Automated unit, integration, security and failure recovery checks. Host-level privilege and live operation are documented separately. |
| 23. Documentation | Installation, AWS IAM, DNS, CLI, API, service integration, migration, recovery and operational guides. |
| 24. Deployment workflow | Mocked issuance, shared wildcard, authorisation, key rotation and rollback passed. Full workflow on administrator-controlled domains is pending. |
| 25. Implementation approach | Incremental commits, documented upstream verification and an explicit decision for the DNS plugin adjustment. |
