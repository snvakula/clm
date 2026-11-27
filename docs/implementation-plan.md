# Implementation plan and verification

The feature branch retains the original foundation. The [specification](specification.md) governs requirements. The branch targets `main` for review.

## Development stages

| Stage | Progress and verification |
|---|---|
| 1. Project foundation and common library | Implemented exact DNS identity, permissions and locking helpers, strict PEM, chain and SAN checks, manifests and bounded ZIP handling. |
| 2. Server core and certificate repository | Implemented SQLite migration v1, transactional metadata, immutable generations, durable publication and reconciliation. |
| 3. ACME and Route 53 integration | Implemented production Let's Encrypt, isolated per-account engine state, public delegation checks, safe TXT changes and migration recovery. Real CA and AWS acceptance is pending. |
| 4. Server CLI and distribution API | Implemented administration, certificate import, client access control, token lifecycle, audits and authenticated, leased distribution. |
| 5. Client application | Implemented enrolment, certificate installation, scheduled updates, local profiles and recoverable grouped activation. |
| 6. Installation and system integration | Added native Debian 13 installers, isolated virtual environments, Nginx, systemd and log rotation. Full privileged install and upgrade acceptance is pending. |
| 7. Testing and security review | Local automated suite, lint, formatting, dependency checks, source patch checks and Nginx configuration checks passed. Production deployment acceptance is pending. |
| 8. Documentation and pull request | Administrator, client and operations guides are complete. The single feature-branch pull request is for review and manual acceptance. |

## Test results

- Python 3.11.18 and Debian 13 Python 3.13.5 each passed all 38 tests, with the pinned upstream engine archive and Debian Nginx 1.26.3 available to the tests.
- Ruff lint and formatting, ShellCheck, Bash syntax and `git diff --check` passed.
- Clean server and client Python builds from their hash-locked requirements passed `pip check`. The client works without the server-only dependencies.
- `pip-audit` reported no known vulnerabilities in the final server and build lockfiles or in the client lockfile. The initially selected `cryptography` version was upgraded to address reported advisories.
- The application served health through an inherited Unix socket. A Nginx configuration test used local certificates. systemd calendar expressions parsed successfully.
- The pinned acme.sh source patch applied with zero fuzz. The mocked Route 53 integration exercised an interrupted DNS response, later cleanup and preservation of an existing TXT value and TTL. The suite used test certificates, mocked external ACME and AWS operations and no real credentials.

## Decisions and known limitations

- Approved deviation in §7.4. Upstream acme.sh 3.1.7 `dns_aws_rm` could delete the whole TXT record set. CLM uses the reviewed, pinned plugin patch, preserving other TXT values with a conditional, transactional change and verifying the exact configured public hosted zone. The patch also keeps AWS signing credentials and challenge values out of process arguments. A simple TXT record set is required at the alias endpoint.
- Nginx is the supported local service profile. The deployment supplies the TLS bootstrap certificate and site ACLs.
- Google Public DNS over HTTPS is the public resolver. Authorised deployment networks must permit the outbound request.
- Imported certificates are eligible for automatic renewal after their first successful explicit renewal through the selected ACME account. An unresolved CA revocation can require operator confirmation from the issuer.
- Multi-SAN certificates, alternative ACME environments and application-level encryption are outside scope. The deployment requires encrypted underlying storage and backups.
- Remaining acceptance work requires clean Debian 13 hosts with root and a running systemd, operator-controlled DNS with scoped AWS credentials, and real Nginx service reloads. Confirm repeated full installations and upgrades, account isolation under the deployed service identity, timers, issued certificate and key rotation, and a full backup restoration. See the [manual acceptance checklist](operations.md#manual-deployment-acceptance).

## Final review against the specification

| Section | Review |
|---|---|
| 1. Project objective | Issuance, renewal, private key rotation, authenticated distribution, local activation and migration are provided by the three entry points. |
| 2. General requirements | Python, Debian 13, SQLite, FastAPI, Nginx, systemd, production Let's Encrypt and Route 53. The implementation uses British English. |
| 3. Architecture | Local administration, distribution through a Unix socket and Nginx, and a pull-only client. |
| 4. Identity and reuse | Canonical IDNA DNS identities, independent wildcard and exact names, unique storage and many-to-many grants. |
| 5. Server directories | Native layout under `/opt/clm`; OS integration files in the applicable system locations. |
| 6. ACME integration | Pinned acme.sh flags verified against source. Renewal eligibility and rotation delegated to the engine with validated export. Live CA acceptance pending. |
| 7. Route 53 | Public CNAME discovery, exact hosted zone, account isolation, global locking, conditional TXT updates and recoverable migration. Live Route 53 acceptance pending. |
| 8. Repository | Validated, immutable PEM generations, authenticated checksums, atomic switching, retention and download leases. |
| 9. Database | SQLite schema version, WAL, busy timeouts, foreign keys, audit trail and publication reconciliation. |
| 10. Administrative CLI | Certificate, client, import, status, check and version commands; JSON inspection and explicit destructive controls. |
| 11. Distribution API | Versioned GET endpoints, scoped authentication, manifests and bundles, sensitive response cache controls. |
| 12. Authentication and security | Hashed random tokens, API identity, rate limits, restricted socket, ACL template, PEM permissions and encrypted-storage guidance. Root deployment checks pending. |
| 13. Client directories | Native layout under `/opt/clm-client`, protected credentials and separate dependency set. |
| 14. Client CLI | Secure enrolment, available certificates, install, inspection, update and guarded removal. |
| 15. Client storage | Versioned generations with stable paths and current and previous retention. |
| 16. Client updates | Bounded manifest and archive verification, serialised switching, service checks, rollback and interrupted update recovery. |
| 17. Profiles | Local-only argument arrays, timeouts, exit status and Nginx check, reload and verification profile. Live Nginx reload pending. |
| 18. Automatic renewal and updates | Persistent systemd timers and UTC schedule settings. Actual timer execution on deployed hosts pending. |
| 19. Logging and monitoring | Structured logs and journal output, rotation, SQLite audit and machine-readable expiry and failure status. |
| 20. Installation and upgrades | Native installers with versioned releases, dependency hashes and state preservation. Full privileged installation, repetition and upgrade pending. |
| 21. Source layout | Shared validation and distribution format with separate server and client entry points, production locks and CI. |
| 22. Testing | Automated unit, integration, security and failure recovery checks. Host-level privilege and live acceptance are documented separately. |
| 23. Documentation | Installation, AWS IAM, DNS, CLI, API, service integration, migration, recovery and operational guides. |
| 24. Acceptance workflow | Mocked issuance, shared wildcard, authorisation, key rotation and rollback passed. Full workflow on administrator-controlled domains is pending. |
| 25. Implementation approach | Incremental commits, documented upstream verification and an explicit decision for the DNS plugin adjustment. |
