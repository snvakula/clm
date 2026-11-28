# Bash client guide

The Bash client runs on Linux with Bash 4.4 or later, curl with HTTPS support, jq 1.6 or later, OpenSSL 1.1.1 or later, Info-ZIP unzip, GNU coreutils, a standard awk and sed, util-linux flock and a system CA trust store. Install these tools using your distribution's package manager. The client requires neither Python nor a particular init system. Use local storage that supports symbolic links, file locking and atomic rename.

From the CLM checkout, run the installer and edit the live configuration as root.

```sh
sudo ./scripts/install-client-bash.sh
sudoedit /opt/clm-client-bash/config/client.conf
```

Enter the HTTPS endpoint and token issued for this host. Leave the configuration root-owned with mode `0600`. Write values without quotes or spaces around the equals sign.

```ini
server=https://clm.example.com
token=YOUR_CLIENT_TOKEN
```

```sh
sudo clm-client-bash update
sudo clm-client-bash status
```

An update downloads and verifies all changed assignments before switching their `current` links. It leaves existing files available when the server is unavailable or withdraws an assignment. It rejects lower generation numbers and retains the active and preceding generations. Use `status --json` for monitoring. The command reports existing certificate locations even when the network is unavailable.

Certificate files are stored under `/opt/clm-client-bash/certificates/`. Published directories use mode `0750` and published files, including private keys, use mode `0640`. Use the stable paths shown by `status` in your service configuration. Restrict membership of any group granted access to the certificate storage. If both clients are installed on a host, they maintain independent configuration and storage directories.

## Scheduling and hooks

On hosts booted with systemd, the installer places its units under `/opt/clm-client-bash/systemd/`, links them into systemd and enables `clm-client-bash-update.timer`. It checks daily between 04:00 and 07:00 UTC. On other hosts, schedule `/usr/local/bin/clm-client-bash update` as root using the local scheduler. For example, use a root crontab entry if cron is installed and running.

```cron
0 5 * * * /usr/local/bin/clm-client-bash update
```

Place root-owned executable hooks in `/opt/clm-client-bash/config/hooks.d/`. Hook names may contain ASCII letters, digits, dots, underscores and hyphens. The hook environment and rollback events follow the [client hook contract](client.md#certificate-update-hooks). The bundled `restart-web-servers` example restarts active Apache and Nginx services on systemd hosts. On other init systems, install a hook with the service commands for that host.

An interrupted update is rolled back on the next update invocation. If rollback hooks repeatedly fail, repair the failing hook and retry with the same storage. See the [operations guide](operations.md#back-up-and-restore) for backup and restore.
