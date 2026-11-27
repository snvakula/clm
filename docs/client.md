# Debian client guide

On Debian 13, install the client and edit its configuration as root.

```sh
sudo ./scripts/install-client-debian.sh
sudoedit /opt/clm-client/config/client.yaml
```

The installer links `/usr/local/bin/clm-client` to `/opt/clm-client/bin/clm-client`. If the name is already in use by another file or link, resolve the conflict before re-running the installer.

The installer sets `/opt/clm-client/certificates/` to mode `0750` on installation and upgrade. Private key files use mode `0600` by default.

Set the HTTPS address of your CLM server and the token assigned to this client. Keep the file owned by root and at mode `0600`.

```yaml
server: https://clm.example.com
token: 'replace-with-your-client-token'
```

When upgrading a registered client, transfer the server URL and token from the old `/opt/clm-client/config/credentials` into `client.yaml`. The same applies to a rotated token.

```sh
sudo /opt/clm-client/bin/clm-client update
sudo /opt/clm-client/bin/clm-client status
```

`update` fetches all the certificates currently assigned to the token. It validates the entire batch before changing the local generation links and skips downloads for unchanged generations. Existing files remain available if the server is unreachable or withdraws a grant. `status` shows installed certificates, expiry dates, full certificate and private key paths, and the last update result. Use `status --json` for monitoring.

The installer stores the timer and service files in `/opt/clm-client/systemd/`, links them from `/etc/systemd/system/`, and enables and starts the timer. The persistent timer checks daily between 04:00 and 07:00 UTC. Use `systemctl edit clm-client-update.timer` to adjust the schedule.

## Certificate update hooks

Place executable root-owned scripts in `/opt/clm-client/config/hooks.d/` and give them mode `0700`. The client runs them by file name once per changed batch, after the new certificate and key paths are active. It skips hooks when the certificate material stays the same. The example `restart-web-servers` hook in that directory restarts running Apache and Nginx services if present. To disable the example hook, remove its execute permission.

A hook receives `CLM_CLIENT_ROOT`, `CLM_HOOK_EVENT` and `CLM_UPDATED_CERTIFICATES`. The last variable lists the changed DNS identifiers separated by newlines. The update event is `updated`. If a hook fails, CLM restores the previous certificate links and calls each attempted hook with `rollback`. Make hooks safe to retry. The client limits each run to 120 seconds and reports failures by script name. Keep script dependencies and any output under your own control. Configure services independently to consume the stable paths shown by `status`.
