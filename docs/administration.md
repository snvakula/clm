# Administrator guide

## Install the server

Use a Debian 13 host with encrypted underlying storage mounted at `/opt/clm` or covering it. Assign an operator-controlled FQDN and arrange a publicly trusted certificate for the Nginx HTTPS endpoint. Allow HTTPS access only from authorised networks or VPN clients. Outbound HTTPS is required for Let's Encrypt, Route 53 and Google Public DNS over HTTPS. Installation downloads packages and a checksum-verified acme.sh source archive. It does not issue certificates or contact AWS.

Run the installer as root from the reviewed checkout.

```sh
sudo ./scripts/install-server.sh
```

The installer places the working configuration in `/opt/clm/config/`. Edit `clm.yaml` for the ACME account contact email, `route53.yaml` for accounts and aliases, and `aws/credentials` for the matching AWS profiles. The Route 53 file includes the mapping layout for two AWS accounts in comments. Re-running the installer preserves configured files.

Edit `/etc/nginx/sites-available/clm`. Set `server_name`, TLS paths and explicit IPv4 and IPv6 network allow rules before `deny all`. Apply corresponding firewall rules. Enable the site using a link in `/etc/nginx/sites-enabled/`, then run `nginx -t` and `systemctl reload nginx`. Use a trusted independent TLS certificate to bootstrap the endpoint. Debian's default Nginx site can be disabled if this host does not serve it.

Load and enable the installed CLM units when the host configuration is ready.

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now clmd.socket clm-renew.timer
```

The socket at `/opt/clm/run/clmd.sock` is accessible to the standard Debian Nginx group `www-data`. There is no application TCP listener.

The installed server package is in `/opt/clm/app/clm_server`. The Python interpreter and dependencies use a single standard virtual environment at `/opt/clm/venv`. The shared acme.sh executable is at `/opt/clm/acme/acme.sh`, and each AWS account keeps its own working state under `/opt/clm/acme/instances/`.

## Configure Route 53 and ACME

Set the operator contact email in `/opt/clm/config/clm.yaml`. CLM passes its only setting, `account_email`, to acme.sh as the contact for the Let’s Encrypt account. Fill in `/opt/clm/config/route53.yaml` using the [two-account example](../examples/server/route53.yaml). Each account has one unique static credentials profile. Each configured alias maps to a single account and public hosted zone. Several aliases can refer to the same account or zone. The initial `{}` values are empty YAML mappings. Replace them with your account and alias mappings.

Enter the access key ID and secret in `/opt/clm/config/aws/credentials` using the [profile example](../examples/server/credentials). Set `aws_session_token` for temporary AWS credentials and replace the credentials before they expire. Keep the directory private and credentials mode `0600`. Use narrowly scoped IAM permissions per account and hosted zone. The [IAM example](../examples/server/iam-policy.json) grants reads on one hosted zone and permits atomic `CREATE` and `DELETE` operations only at one named TXT endpoint. Repeat the scoped policy for additional endpoints. Protect shared challenge endpoints from other automatic writers.

The alias is specified without the `_acme-challenge.` prefix in CLM. Create CNAME records from the exact challenge names in the domains you wish to validate. The apex and wildcard under the same domain share the challenge record but remain distinct certificates. Subdomains need their own CNAME.

```dns
_acme-challenge.example.com.     CNAME _acme-challenge.acme.example.com.
_acme-challenge.git.example.com. CNAME _acme-challenge.acme.example.com.
_acme-challenge.example.net.     CNAME _acme-challenge.acme.example.org.
```

The target must terminate in the public hosted zone named in the configuration. CLM checks the public NS delegation against Route 53 and rejects an endpoint below a more specific zone. Existing simple TXT values and their TTL are preserved. Routing-policy TXT record sets are rejected. Route 53 applies the conditional deletion of the exact old record set and creation of the replacement in a single change batch. If another writer changes the record set between the read and the transaction, the change fails safely.

Run `sudo clm check` after configuration. The check validates local configuration, credential permissions, the database and active repository contents. Issuance validates the live public DNS delegation and Route 53 zone. The pinned engine is installed without invoking the native installer or enabling native renewal.

## Administrative CLI

Run commands as root. Quote wildcard identifiers in the shell. Commands show progress on stderr during DNS lookup, issuance, validation and publication. Inspection commands display labelled records by default and keep JSON on stdout when requested. Successful operations return zero. Usage errors return two. A failed operation returns one.

| Command | Behaviour |
|---|---|
| `clm cert issue DNS_NAME [--key ALGORITHM]` | Issues or repairs one certificate. Reuses a valid existing certificate. |
| `clm cert list [--json]`, `clm cert show DNS_NAME [--json]` | Reports identity, status, generation, validity and account metadata. |
| `clm cert renew DNS_NAME [--force]`, `clm cert renew --all [--force]` | Lets acme.sh determine eligibility. Force only for an explicitly authorised reissuance. A failed certificate does not stop the batch. |
| `clm cert rotate-key DNS_NAME [--key ALGORITHM]` | Forces reissuance with a new private key. Allows an explicit algorithm change. |
| `clm cert revoke DNS_NAME` | Revokes the matching current ACME-managed certificate and stops distribution. Preserves repository files. |
| `clm cert delete DNS_NAME [--force]` | Deletes stored generations and assignments. Active assignments require `--force`. This command does not request CA revocation. |
| `clm cert import DNS_NAME DIRECTORY` | Validates the four standard PEM files and publishes a generation. |
| `clm client add CLIENT`, `clm client rotate-token CLIENT` | Prints a new random bearer token once to stdout. Rotation invalidates the prior token. |
| `clm client grant CLIENT DNS_NAME`, `clm client deny CLIENT DNS_NAME` | Grants after obtaining valid material or removes a grant immediately. |
| `clm client disable CLIENT`, `clm client enable CLIENT`, `clm client delete CLIENT` | Changes access immediately. Deleting a client removes all its grants. |
| `clm client list [--json]`, `clm client show CLIENT [--json]` | Shows client names, access and activity without token hashes. |
| `clm status [--json]` | Reports certificate expiry, renewal attempts and repository inconsistencies. |
| `clm check`, `clm version` | Runs local checks or prints the application version. |

Supported key algorithms are `ec-256` (default), `ec-384`, `rsa-2048`, `rsa-3072` and `rsa-4096`. Non-forced renewal honours acme.sh exit status 2 as a skipped operation. Avoid forced retries during CA rate limiting.

## Import existing certificates

Prepare a root-only directory on encrypted storage containing `cert.pem`, `chain.pem`, `fullchain.pem` and `privkey.pem` in standard PEM format. Keep the private key mode `0600`. The leaf must have exactly the requested DNS SAN and chain to the system trust store.

```sh
sudo clm cert import git.example.com /path/to/protected/migration
sudo clm client grant git git.example.com
```

The imported generation is available through existing grants. To enrol it for automatic renewal, configure DNS and credentials, then explicitly run `sudo clm cert renew git.example.com` and check the account and generation using `clm cert show`. A newly issued, validated ACME generation establishes the account's renewal state. Remove the protected migration copies once the transition is verified.

## Distribution API

Send `Authorization: Bearer TOKEN` on each protected request over HTTPS. The API never uses query parameters for credentials. All API responses carry `Cache-Control: no-store`.

| Request | Response |
|---|---|
| `GET /v1/health` | Minimal JSON health response. |
| `GET /v1/client` | Authenticated client name. |
| `GET /v1/certificates` | JSON with only assigned, valid certificates. Each entry contains `certificate_id`, URL-safe `id`, `version` and `not_after`. |
| `GET /v1/certificates/{id}/manifest` | Current manifest containing exact DNS identity, version, certificate metadata and SHA-256 checksums. |
| `GET /v1/certificates/{id}/versions/{version}/bundle` | An uncompressed ZIP containing only `cert.pem`, `chain.pem`, `fullchain.pem`, `privkey.pem` and `manifest.json`. |

Fetch the manifest first and use its generation to download the immutable bundle. Each manifest grants the requesting token a five-minute lease for that generation, so an authorised download can finish across a publication. Grant removal, disablement or token rotation invalidates access immediately. A missing or unassigned resource returns `404`. Invalid credentials return `401`, excessive attempts return `429` and an unavailable repository returns `503`.
