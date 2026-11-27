# Implementation plan

All development takes place on the feature branch; merge requires review.

1. Project foundation and common library — in progress.
2. Server core and certificate repository.
3. ACME and Route 53 integration.
4. Server CLI and distribution API.
5. Client application.
6. Installation and system integration.
7. Integration testing and security review.
8. Documentation and pull request.

## Stage 1 progress

Initial packaging, DNS identifier normalisation, POSIX locking, unit tests and CI configuration added. Certificate validation, manifest and archive handling, configuration validation and filesystem primitives remain outstanding. CI results must be verified before marking this stage complete.

No live ACME or AWS operations are permitted in automated tests.
