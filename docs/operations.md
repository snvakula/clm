# Operations and recovery

## Protect the server and clients

- Use encrypted underlying storage and encrypted backups. CLM stores all private keys, including ACME account and working keys, as standard PEM files. Disk encryption does not protect against a compromised running VM, privileged OS accounts or a compromised application.
- Keep `/opt/clm/config/aws` and `/opt/clm/acme/instances` inaccessible to the API identity. Treat the whole ACME instance as sensitive, including working state, recovery journals and old keys. On clients, keep `/opt/clm-client/config` and certificate storage private.
- Published server material is root owned. The `clm-api` group receives read access to the repository, including generation keys at mode `0640`. AWS credentials, ACME private keys and client credentials are owner-only. The API has a writable SQLite database for activity and audits, and a read-only certificate mount. Keep the API identity unique to CLM.
- Keep TLS certificate verification enabled and restrict network access with both Nginx allow rules and the host firewall. Review proxy and forwarding configuration before deploying a second proxy. Never add an access log format containing request credentials.
- Never enable native acme.sh cron, debug or upgrade jobs in CLM instances. Upgrade the pinned engine and patch together after reviewing upstream changes to supported flags, renewal and DNS cleanup.
- Limit every IAM credential to its exact hosted zones and TXT endpoints. Apply normal Debian security updates. Periodically inspect access assignments and rotate tokens and AWS credentials through a protected channel.

## Monitor and troubleshoot

The server renewal timer starts from 02:00 UTC with up to 30 minutes of random delay. The client update timer starts from 04:00 UTC with up to three hours of random delay. Both are persistent. Change schedules with systemd drop-ins.

Use `clm status --json` and `clm-client status --json` for Zabbix or another monitoring system. Check expiry, the last renewal or update attempt, recorded failures and repository inconsistencies. Use a privileged local collection method that exposes only the health information your monitor requires. Alert on failed units as well, which catches errors before CLM can write status. Application logs are under each application's `logs/` directory and are rotated weekly. The journal also captures service output. The server database holds the audit trail, with download activity, authentication failures, access changes and certificate operations. Plan capacity and the retention of encrypted database backups.

| Problem | What to check |
|---|---|
| Missing or ambiguous delegation | Check the exact public `_acme-challenge` CNAME for the identifier and the final target against the alias configuration. The hostname's own record is required. |
| Route 53 or ACME failure | Check zone NS delegation, credentials and session expiry, scoped IAM and CA rate limits. Supply the original account and alias configuration if a pending cleanup requires it. Check for external writers on shared endpoints. |
| Reverse proxy errors | Run `nginx -t`, inspect `systemctl status clmd.socket clmd.service` and check the proxy socket group. Preserve HTTPS termination and network allow rules. |
| Client authentication or grant fails | Confirm the client is enabled, the token is current and the certificate is granted and valid. Re-run `clm-client init` with a newly rotated token if needed. |
| Client service activation fails | Check the local service configuration and profile, then re-run `clm-client update`. Inspect any retained journal and generation paths printed by the command before manual recovery. |
| Interrupted publication | Run `clm check` to reconcile pending filesystem and database state. Inspect the existing files and use the consistent backup if reconciliation reports damage. The API reads complete versions from the database association. |
| Certificate cannot be revoked | Verify that the current published certificate is the one present in the configured ACME instance. If an imported certificate or an interrupted CA revocation cannot be confirmed by CLM, resolve it with its issuer using the original certificate and issuing account. |

For a DNS operation interrupted after a Route 53 request, preserve the per-account `dns-operation.json` and its original alias configuration. Retry the original operation so CLM can reconcile the record. Check Route 53 for other values before any manual remediation. The journal is sensitive and should not be pasted into tickets or logs.

## Client compromise

1. Immediately disable the affected client or revoke its grants. Rotate its token before enabling it again and investigate where the token may have been copied.
2. Identify every certificate and key the client could have obtained using assignments and server audit records. Disabling a token cannot invalidate existing private key copies.
3. Reissue each affected certificate with `clm cert rotate-key` and distribute the new generation to trusted clients. Revoke the compromised certificate at the CA. If the affected generation is still current and active, use `clm cert revoke` before rotating, and coordinate the resulting distribution gap. Otherwise use the issuer's procedure to revoke the prior generation. Verify the new serial and successful service reload on every client.
4. Protect and review retained past generations, logs and backups as part of the incident. If the CLM server or AWS credentials may have been compromised, rotate AWS credentials, review DNS changes and reissue every potentially affected certificate.

## Back up and restore

Establish your recovery point and recovery time objectives and routinely practise restoration on an isolated Debian 13 host. The archives contain live secrets. Create them with mode `0600` on encrypted backup storage, restrict who can restore them, and keep off-host copies according to policy.

For a consistent server backup, stop `clm-renew.timer`, allow any running renewal to finish, stop `clmd.socket` and `clmd.service`, and prevent manual commands during the snapshot. Copy `config`, `data` and `acme/instances` together, preserving modes, ownership and symlinks. Include application logs and the system Nginx site, TLS material, firewall configuration and systemd drop-ins if needed for a full host recovery. For example, after quiescing the server:

```sh
umask 077
tar --acls --xattrs -C /opt/clm -cpf /path/on/encrypted-storage/clm-backup.tar \
    config data acme/instances
```

Start the socket and timer again after the archive completes. For clients, stop the update timer and allow any live update to finish. Archive `config`, `data` and `certificates` together. Back up the consuming service configuration separately.

To restore on a new host, install the reviewed release and stop its CLM units before moving data into place. Inspect the archive listing and unpack in an empty root-only directory on encrypted storage. Restore the directories as a matched set, keeping the fresh empty directories until verification succeeds. Map the archived distribution group to the destination `clm-api` GID if it changed. Preserve the server data directory's sticky and setgid bits, private instance access and certificate ownership. Restore clients as root without dereferencing `current` links. Reconcile with `clm check`, check expiry and status, run `nginx -t`, and then start the server socket and timer. Verify an authorised client against HTTPS. For each client, verify the current paths and local service configuration before restarting its update timer.

An older server backup can leave clients ahead of the restored generation counter. The client rejects a lower generation. Recover a newer consistent server backup where possible. Otherwise coordinate with the service operator to detach and reinstall the certificate safely, retaining the existing working files until the replacement is ready.

## Upgrade and remove

Before an upgrade, take a consistent backup and record the installed release and database schema with the date. Stop the relevant CLM services and timers, then run the appropriate installer from the new reviewed checkout. The installer updates the application in `app/` and dependencies in the single `venv/`, and runs the server schema migrator. It preserves configured settings, credentials, certificate state and assignments. When upgrading from the old release-directory layout, it removes the old managed application and virtual environments. On the server it also moves the shared acme.sh installation to `acme/` while preserving `acme/instances/`. After upgrading, run `systemctl daemon-reload` and start the stopped CLM units. Re-run local checks after upgrading.

If a code rollback is necessary, stop the CLM services and timers. Reinstall the previous reviewed checkout to restore matching system units and engine. If the database schema changed or the two components are incompatible, restore the pre-upgrade backup as a consistent set before restarting. Never downgrade the running database manually. The current schema is version 1 and unknown versions are refused.

To uninstall the server, disable and stop the renewal timer and API socket, stop any corresponding services, and remove the CLM units, logrotate entry, Nginx site and CLI links. Reload systemd and Nginx after validating Nginx configuration. To uninstall a client, stop and disable its update timer and service, then remove the CLM units, logrotate entry and CLI link. Verify or migrate any consuming service before removing its certificate files. Both procedures leave the application tree and data available for recovery. Remove them separately only after confirming the backups and the impact on all deployments.

## Manual deployment acceptance

Use clean Debian 13 server and client hosts or VMs with systemd as PID 1. Run fresh installs twice and then upgrade from the same data set. Compare credential modes, installed grants, keys, release links and timer inventory. Confirm that `clm-api` can read published key files through its group access but cannot read AWS credentials, ACME account keys or alter the certificate repository. Confirm the only backend listener is the Unix socket and that Nginx authorises the intended network ranges.

With designated administrator-controlled domains and authorised scoped AWS credentials, run the workflow in [specification §24](specification.md#24-acceptance-criteria). Verify the apex or wildcard and exact hostname remain separate, two clients receive the same assigned wildcard, and a client cannot obtain an unassigned certificate. Exercise a second AWS account, move a CNAME to a different configured account, and verify the new association after successful publication. Use a harmless TXT value at a test endpoint and verify that it and its TTL survive issuance and cleanup. Check a non-forced renewal and an explicitly authorised key rotation, and compare active serials and public keys.

On clients, exercise a successful Nginx reload and a deliberately failing local test profile to verify rollback. Confirm the expected time windows using `systemctl list-timers`. Finally, restore the test backups onto fresh hosts and repeat the authenticated download and service checks.

## Update dependencies

Update Python pins in `pyproject.toml`, regenerate the Python 3.13 server, client and build locks with hashes, then run the test suite and both dependency audits. Review transitive changes as well. For acme.sh, compare the release's help and source, review the patch against the new release, pin the commit and archive checksum, and repeat the offline engine and manual deployment checks before shipping.

Upstream references include the [acme.sh command documentation](https://github.com/acmesh-official/acme.sh/wiki/How-to-issue-a-cert), [challenge alias mode](https://github.com/acmesh-official/acme.sh/wiki/DNS-alias-mode), [pinned engine source](https://github.com/acmesh-official/acme.sh/tree/5567d7fe7298fd37aca53194885ba444c6678669), [Route 53 transactional changes](https://docs.aws.amazon.com/Route53/latest/APIReference/API_ChangeResourceRecordSets.html) and [AWS Signature Version 4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv-create-signed-request.html).
