# Development instructions

- Treat [the technical specification](docs/specification.md) as the authoritative requirements. Report conflicts and obtain approval before changing the agreed architecture.
- Keep the product organisation-independent. Use reserved example domains and never commit deployment-specific credentials or secrets.
- Use Python 3 with type annotations for the server and Debian client. Use Bash for the standalone Linux client. Use British English and minimal, justified dependencies. Keep server and client dependencies separate.
- Verify documented acme.sh behaviour before implementing integrations. Never invent options or silently substitute components.
- Use mocked ACME and AWS operations in automated tests. Never contact production ACME or modify real DNS during automated testing, installation or upgrades.
- Preserve data, permissions, atomic publication, rollback and account isolation. Never log secrets. Clean up unnecessary temporary artefacts.
- Run relevant tests and lint checks before completing changes; report failures and unverified behaviour. Update documentation alongside functionality.
- Do not deliver placeholders for core functionality.
