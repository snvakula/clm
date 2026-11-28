# Linux client guide

Install Bash, Python 3.11 or later with `venv` and `ensurepip`, the OpenSSL command, system CA certificates, GNU coreutils and util-linux `flock` using your distribution's package manager. On Debian, Python's virtual environment support is supplied by `python3-venv`. The installer uses a dedicated virtual environment and verifies Python dependency hashes. Platforms without wheels for the pinned versions may need the packages' native build tools, including a C and Rust toolchain.

Run the installer from the repository and edit the live configuration as root.

```sh
sudo ./scripts/install-client-linux.sh
sudoedit /opt/clm-client/config/client.yaml
```

The installer links `/usr/local/bin/clm-client` to `/opt/clm-client/bin/clm-client`. If the name is already in use by another file or link, resolve the conflict before re-running the installer.

The certificate storage directory `/opt/clm-client/certificates/` and its subdirectories are root-owned with mode `0700`. Certificates, private keys and manifests use mode `0600`. Configure consuming services to read the files with the required local privileges.

Set the HTTPS address of your CLM server and the token assigned to this client. Keep the file owned by root and at mode `0600`.

```yaml
server: https://clm.example.com
token: 'replace-with-your-client-token'
```

After rotating a client's token, update `client.yaml` with the replacement. The client uses the host CA store to verify both the server's HTTPS connection and the downloaded certificates.

```sh
sudo /opt/clm-client/bin/clm-client update
sudo /opt/clm-client/bin/clm-client status
```

`update` fetches all the certificates currently assigned to the token. It validates the entire batch before changing the local generation links and skips downloads for unchanged generations. Existing files remain available if the server is unreachable or withdraws a grant. `status` shows installed certificates, expiry dates, full certificate and private key paths, and the last update result. Use `status --json` for monitoring.

On hosts running systemd, the installer stores the timer and service files in `/opt/clm-client/systemd/`, links them from `/etc/systemd/system/`, and enables and starts the timer. The persistent timer checks daily between 04:00 and 07:00 UTC. Use `systemctl edit clm-client-update.timer` to adjust the schedule.

On other hosts, schedule `/opt/clm-client/bin/clm-client update` as root using the host scheduler. For example, a root crontab can run it once a day at 05:00 local time.

```cron
0 5 * * * /opt/clm-client/bin/clm-client update
```

The installer configures logrotate when it is installed. Otherwise, rotate `/opt/clm-client/logs/clm-client.log` with your host's logging tools.

## Certificate update hooks

Place executable root-owned scripts in `/opt/clm-client/config/hooks.d/` and give them mode `0700`. The client runs them by file name once per changed batch, after the new certificate and key paths are active. It skips hooks when the certificate material stays the same. The example `restart-web-servers` hook restarts running Apache and Nginx services through systemd, OpenRC or a compatible `service` command. To disable the example hook, remove its execute permission.

A hook receives `CLM_CLIENT_ROOT`, `CLM_HOOK_EVENT` and `CLM_UPDATED_CERTIFICATES`. The last variable lists the changed DNS identifiers separated by newlines. The update event is `updated`. If a hook fails, CLM restores the previous certificate links and calls each attempted hook with `rollback`. Make hooks safe to retry. The client limits each run to 120 seconds and reports failures by script name. Keep script dependencies and any output under your own control. Configure services independently to consume the stable paths shown by `status`.

### Nginx certificate snippets

On an Nginx host, enable the optional hook from the checkout that supplied your client.

```sh
sudo install -m 0700 examples/client/hooks.d/nginx-snippets \
    /opt/clm-client/config/hooks.d/nginx-snippets
```

The hook writes `/etc/nginx/snippets/clm/example.com.conf` for `example.com` and `/etc/nginx/snippets/clm/_wildcard.example.com.conf` for `*.example.com`. Very long names that cannot fit in a Linux filename use `_sha256-<digest>.conf`, with the hexadecimal SHA-256 of the canonical certificate identifier.

Include the chosen snippet inside the Nginx `server` block.

```nginx
include /etc/nginx/snippets/clm/example.com.conf;
```

The snippet references the active certificate and private key through the client's stable `current` paths. The hook leaves existing operator files in place and removes a generated snippet if rollback leaves it without a certificate. When both example hooks are enabled, `nginx-snippets` runs before `restart-web-servers`.

For a certificate the client already has, generate its snippet once before adding the Nginx include. Use the identifier from `clm-client status`.

```sh
sudo env CLM_CLIENT_ROOT=/opt/clm-client CLM_HOOK_EVENT=updated \
    CLM_UPDATED_CERTIFICATES=example.com /opt/clm-client/config/hooks.d/nginx-snippets
```

After editing the Nginx configuration, check it with `sudo nginx -t` and reload Nginx through your service manager.
