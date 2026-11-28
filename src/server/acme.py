"""Run the pinned acme.sh engine with isolated account state."""

import os
import signal
import subprocess
import sys
from pathlib import Path

from .certificate import PEMS, read_material

from .dns_records import entry
from .route53 import Route53, Target

PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
LENGTHS = {
    "ec-256": "ec-256",
    "ec-384": "ec-384",
    "rsa-2048": "2048",
    "rsa-3072": "3072",
    "rsa-4096": "4096",
}


class Acme:
    def __init__(self, root: Path, route: Route53, email: str) -> None:
        self.root = root
        self.route = route
        self.email = email
        self.engine = root / "acme"
        self.lock_descriptor: int | None = None

    def instance(self, account: str) -> Path:
        if account not in self.route.accounts:
            raise ValueError("Unknown ACME account")
        instance = self.root / "acme/instances" / account
        for path in (
            instance,
            *(
                instance / name
                for name in (
                    "config",
                    "certificates",
                    "logs",
                    "work",
                    "exports",
                )
            ),
        ):
            path.mkdir(mode=0o700, exist_ok=True)
            if (
                path.is_symlink()
                or path.stat().st_uid != os.geteuid()
                or path.stat().st_mode & 0o077
            ):
                raise ValueError("Unsafe ACME instance permissions")
        return instance

    def _environment(self, target: Target) -> dict[str, str]:
        instance = self.instance(target.account)
        return {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "HOME": str(instance / "work"),
            "TMPDIR": str(instance / "work"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "CLM_PYTHON": sys.executable,
            "CLM_DNS_MODULE": f"{__package__}.dns_records",
            "CLM_HOSTED_ZONE": target.zone,
            "CLM_CHALLENGE_RECORD": target.record,
            "CLM_DNS_JOURNAL": str(instance / "dns-operation.json"),
            **self.route.credentials(target.account),
        }

    def _run(self, args: list[str], env: dict[str, str], operation: str) -> int:
        descriptors = () if self.lock_descriptor is None else (self.lock_descriptor,)
        try:
            with subprocess.Popen(
                args,
                env=env,
                cwd=env["HOME"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                umask=0o077,
                pass_fds=descriptors,
                start_new_session=True,
            ) as process:
                try:
                    status = process.wait(timeout=1200)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
        except subprocess.TimeoutExpired:
            raise ValueError(f"ACME {operation} timed out") from None
        except OSError:
            raise ValueError(f"Could not execute ACME {operation}") from None
        if status != 0 and not (operation == "renew" and status == 2):
            raise ValueError(f"ACME {operation} failed with exit status {status}")
        return status

    def recover_dns(self) -> None:
        instances = self.root / "acme/instances"
        for path in sorted(instances.glob("*/dns-operation.json")):
            account = path.parent.name
            saved = entry(path)
            target = self.route.aliases.get(saved["record"].removeprefix("_acme-challenge."))
            if target is None or target.account != account or target.zone != saved["zone"]:
                raise ValueError("Restore the original Route 53 configuration for DNS recovery")
            self._run(
                ["/bin/sh", str(self.engine / "recover-dns.sh"), str(self.engine)],
                self._environment(target),
                "DNS cleanup",
            )

    def _command(
        self,
        operation: str,
        target: Target,
        identifier: str,
        algorithm: str,
        arguments: list[str],
    ) -> int:
        instance = self.instance(target.account)
        options = [
            "--home",
            str(instance / "work"),
            "--config-home",
            str(instance / "config"),
            "--cert-home",
            str(instance / "certificates"),
            "--server",
            PRODUCTION,
            "--accountemail",
            self.email,
        ]
        if operation != "issue" and algorithm.startswith("ec-"):
            options.append("--ecc")
        return self._run(
            [
                str(self.engine / "acme.sh"),
                f"--{operation}",
                "--domain",
                identifier,
                *options,
                *arguments,
            ],
            self._environment(target),
            operation,
        )

    def obtain(
        self,
        target: Target,
        identifier: str,
        algorithm: str,
        *,
        issue: bool,
        force: bool,
    ) -> bool:
        self.recover_dns()
        operation = "issue" if issue else "renew"
        options = ["--always-force-new-domain-key"]
        if issue:
            options += [
                "--dns",
                "dns_aws",
                "--challenge-alias",
                target.alias,
                "--keylength",
                LENGTHS[algorithm],
            ]
        if force:
            options.append("--force")
        try:
            status = self._command(operation, target, identifier, algorithm, options)
        finally:
            self.recover_dns()
        return status == 0

    def export(self, target: Target, identifier: str, algorithm: str) -> dict[str, bytes]:
        directory = self.instance(target.account) / "exports"
        paths = [directory / name for name in PEMS]
        flags = ("--cert-file", "--ca-file", "--fullchain-file", "--key-file")
        try:
            for path in paths:
                path.unlink(missing_ok=True)
            options = [part for flag, path in zip(flags, paths) for part in (flag, str(path))]
            self._command("install-cert", target, identifier, algorithm, options)
            return read_material(directory)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)

    def revoke(self, target: Target, identifier: str, algorithm: str) -> None:
        self.recover_dns()
        self._command("revoke", target, identifier, algorithm, [])

    def ready(self) -> bool:
        plugin = self.engine / "dnsapi/dns_aws.sh"
        return (
            os.access(self.engine / "acme.sh", os.X_OK)
            and (self.engine / "recover-dns.sh").is_file()
            and plugin.is_file()
            and b"dns_aws_clm_recover()" in plugin.read_bytes()
        )
