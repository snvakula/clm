# Client guide

## Install and enrol

On Debian 13, run the client installer from the reviewed checkout.

```sh
sudo ./scripts/install-client.sh
sudo clm-client init https://clm.example.com
sudo clm-client available
sudo systemctl daemon-reload
sudo systemctl enable --now clm-client-update.timer
```

The installer places the application and its single Python virtual environment in `/opt/clm-client/app` and `/opt/clm-client/venv`. It writes the active settings to `/opt/clm-client/config/client.yaml` and the Nginx profile to `/opt/clm-client/config/services.d/nginx.yaml`. The CLM administrator must provide a token for this client over a protected channel. The interactive prompt does not echo it. Automated provisioning can use `--credentials-file /path/to/protected/token` or `--token-stdin`. A credentials file must be readable only by its owner. The credential is verified through HTTPS before CLM saves it under `/opt/clm-client/config/credentials`. Rerun `init` after an administrator rotates the token.

## Install and use a certificate

```sh
sudo clm-client install git.example.com --service nginx
sudo clm-client show git.example.com
```

Use the paths returned by the command. Configure Nginx yourself, then test and gracefully reload it.

```nginx
ssl_certificate /opt/clm-client/certificates/<storage-id>/current/fullchain.pem;
ssl_certificate_key /opt/clm-client/certificates/<storage-id>/current/privkey.pem;
```

```sh
sudo nginx -t
sudo systemctl reload nginx
```

The standard Nginx master runs as root and can open the mode `0600` private key. A deployment that runs a master unprivileged must arrange narrowly scoped local access to the selected certificate's paths and take ownership of checking that access on subsequent updates. The client manages registered certificates and generation links. It never rewrites Nginx or other service configuration.

## Commands and service profiles

| Command | Behaviour |
|---|---|
| `clm-client available [--json]` | Lists valid certificates the server authorises. |
| `clm-client install DNS_NAME [--service PROFILE]` | Installs or updates the certificate and registers it for scheduled updates. Repeat `--service` to register multiple profiles. |
| `clm-client list [--json]`, `clm-client show DNS_NAME [--json]` | Reports local certificate paths, validity and associations. |
| `clm-client update [DNS_NAME]` | Checks for changed generations. Omitting the name checks every installed certificate. |
| `clm-client remove DNS_NAME [--force]` | Removes a certificate. Detach it from service configuration before confirming removal with `--force`. |
| `clm-client status [--json]`, `clm-client version` | Reports update status and repository health, or prints the version. |

Locally trusted administrators can create profiles under `/opt/clm-client/config/services.d/` with `name`, `check`, `reload`, optional `verify` and an optional per-command `timeout` from 1 to 300 seconds. Commands are argument arrays executed as the local update user without a shell. Profiles must be owned by the authorised local account or root and protected from other writers. The installer supplies the [Nginx profile](../examples/client/services.d/nginx.yaml).

The updater fetches manifests first and skips unchanged generations. It fully validates archives and certificates before switching stable links. A batch validates affected service configuration before reloading each service once. On failure it restores the previous links, revalidates and reloads the previous configuration. If rollback fails, it preserves the recovery journal and generation files. When the server is unavailable or denies access, installed files remain in place.

The persistent timer checks between 04:00 and 07:00 UTC, with a random delay. Use `systemctl edit clm-client-update.timer` for deployment-specific scheduling.
