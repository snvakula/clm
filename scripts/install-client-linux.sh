#!/usr/bin/env bash

keep_configuration() {
    local mode=$1 source=$2 target=$3
    if [[ ! -e $target && ! -L $target ]]; then
        install -m "$mode" -- "$source" "$target"
    fi
}

install_client_storage() {
    local root=$1
    install -d -m 0700 "$root/certificates" "$root/data" "$root/logs"
    chmod 00700 "$root/certificates"
}

install_client_configuration() {
    local sources=$1 config=$2
    install -d -m 0700 "$config/hooks.d"
    keep_configuration 0600 "$sources/examples/client/client.yaml" "$config/client.yaml"
}

stage_client_files() {
    local sources=$1 workspace=$2 interpreter=$3
    "$interpreter" - "$sources" "$workspace" "$interpreter" <<'PYTHON'
import shutil
import sys
import tomllib
from pathlib import Path

source, workspace, interpreter = map(Path, sys.argv[1:])
package = source / "src/client_linux"
metadata = tomllib.loads((source / "pyproject.toml").read_text())["project"]
app = workspace / "app"
app.mkdir()
shutil.copyfile(source / "LICENSE", app / "LICENSE")
for module in package.glob("*.py"):
    shutil.copyfile(module, app / module.name)
with (app / "__init__.py").open("a") as module:
    module.write(f"\n__version__ = {metadata['version']!r}\n")

commands = workspace / "bin"
commands.mkdir()
for name in ('clm-client',):
    entry, function = metadata["scripts"][name].split(":")
    module = entry.split(".", 1)[1]
    (commands / name).write_text(
        f"#!{interpreter} -B\n"
        f"from app.{module} import {function}\n"
        f"raise SystemExit({function}())\n"
    )
PYTHON
}

prepare_client_environment() {
    local sources=$1 root=$2 workspace=$3
    local environment="$root/venv" site_packages
    if [[ ! -x $environment/bin/python ]] ||
        ! "$environment/bin/python" -m pip --version >/dev/null 2>&1; then
        python3 -m venv "$environment"
    fi
    "$environment/bin/python" -m pip --disable-pip-version-check install \
        --require-hashes -r "$sources/packaging/requirements/client.txt"
    "$environment/bin/python" -m pip check
    stage_client_files "$sources" "$workspace" "$environment/bin/python"
    site_packages=$("$environment/bin/python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
    printf '%s\n' "$root" > "$workspace/clm-application.pth"
    install -m 0644 -- "$workspace/clm-application.pth" "$site_packages/clm-application.pth"
    chmod -R a+rX -- "$environment" "$workspace/app"
}

publish_application() {
    local target=$1 workspace=$2
    if [[ -e $target || -L $target ]]; then
        mv -T -- "$target" "$workspace/old-application"
    fi
    if ! mv -T -- "$workspace/app" "$target"; then
        if [[ -e $workspace/old-application || -L $workspace/old-application ]]; then
            mv -T -- "$workspace/old-application" "$target"
        fi
        return 1
    fi
}

install_cli_link() {
    local link=$1 destination=$2
    if [[ -L $link && $(readlink -- "$link") == "$destination" ]]; then
        return
    fi
    ln -sT -- "$destination" "$link"
}

install_client_units() {
    local sources=$1 root=$2 system_units=$3 unit
    install -d -m 0755 "$root/systemd"
    for unit in clm-client-update.timer clm-client-update.service; do
        install -m 0644 -- "$sources/packaging/systemd/$unit" "$root/systemd/$unit"
        ln -sfn -- "$root/systemd/$unit" "$system_units/$unit.new"
        mv -Tf -- "$system_units/$unit.new" "$system_units/$unit"
    done
    systemctl daemon-reload
    systemctl enable --now clm-client-update.timer
}

check_client_dependencies() {
    local tool
    for tool in python3 openssl flock install mktemp stat mv ln; do
        if ! command -v "$tool" >/dev/null; then
            printf 'Required command is missing: %s\n' "$tool" >&2
            return 1
        fi
    done
    python3 - <<'PY'
import ssl
import sys

if sys.platform != "linux" or sys.version_info < (3, 11):
    raise SystemExit("The CLM client requires Linux and Python 3.11 or later.")
try:
    import ensurepip
    import venv
except ImportError:
    raise SystemExit("Install Python virtual environment support with ensurepip.") from None
trust = ssl.get_default_verify_paths()
if not trust.cafile and not trust.capath:
    raise SystemExit("Install a system CA trust store before installing the CLM client.")
PY
    openssl version >/dev/null
}

install_client() {
    set -euo pipefail
    umask 077
    local root=/opt/clm-client sources
    if (($#)); then
        echo 'Run install-client-linux.sh without arguments.' >&2
        exit 2
    fi
    if [[ $EUID != 0 ]]; then
        echo 'Run the installer as root.' >&2
        exit 1
    fi
    check_client_dependencies
    if [[ -L $root || (-e $root && $(stat -c %u -- "$root") != 0) ]]; then
        echo 'The application directory must be owned by root and may not be a symbolic link.' >&2
        exit 1
    fi

    sources=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    install -d -m 0755 "$root" "$root/bin" "$root/venv"
    install -d -m 0711 "$root/run"
    install -d -m 0700 "$root/config"
    exec 9>"$root/run/install.lock"
    flock -x 9
    workspace=$(mktemp -d "$root/run/install.XXXXXXXX")
    trap 'rm -rf -- "$workspace"' EXIT

    keep_configuration 0600 /dev/null "$root/run/update.lock"
    exec 8<>"$root/run/update.lock"
    flock -x 8
    install_client_storage "$root"
    install_client_configuration "$sources" "$root/config"
    prepare_client_environment "$sources" "$root" "$workspace"
    if command -v logrotate >/dev/null && [[ -d /etc/logrotate.d ]]; then
        install -m 0644 "$sources/packaging/logrotate/clm-client" /etc/logrotate.d/clm-client
    fi
    publish_application "$root/app" "$workspace"
    install -m 0755 -- "$workspace/bin/clm-client" "$root/bin/clm-client"
    (umask 022; mkdir -p /usr/local/bin)
    install_cli_link /usr/local/bin/clm-client "$root/bin/clm-client"
    flock -u 8
    if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null; then
        install_client_units "$sources" "$root" /etc/systemd/system
    fi
    echo 'Installed CLM Linux client. Configuration directory /opt/clm-client/config.'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    install_client "$@"
fi
