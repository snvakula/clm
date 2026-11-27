#!/bin/bash

keep_configuration() {
    local mode=$1 source=$2 target=$3
    if [[ ! -e $target && ! -L $target ]]; then
        install -m "$mode" -- "$source" "$target"
    fi
}

install_client_configuration() {
    local sources=$1 config=$2
    install -d -m 0700 "$config/services.d"
    keep_configuration 0600 "$sources/examples/client/client.yaml" "$config/client.yaml"
    keep_configuration 0600 "$sources/examples/client/services.d/nginx.yaml" \
        "$config/services.d/nginx.yaml"
}

stage_client_files() {
    local sources=$1 workspace=$2 interpreter=$3
    "$interpreter" - "$sources" "$workspace" "$interpreter" <<'PYTHON'
import shutil
import sys
import tomllib
from pathlib import Path

source, workspace, interpreter = map(Path, sys.argv[1:])
package = source / "src/clm_client_debian"
metadata = tomllib.loads((source / "pyproject.toml").read_text())["project"]
app = workspace / "app"
app.mkdir()
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
    if [[ ! -f $environment/pyvenv.cfg ]]; then
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

remove_legacy_layout() {
    local root=$1 entry name
    for entry in "$root/venv/"*; do
        name=${entry##*/}
        if [[ $name =~ ^[a-f0-9]{64}$ && ! -L $entry && -f $entry/pyvenv.cfg ]]; then
            rm -rf -- "$entry"
        fi
    done
}

switch_link() {
    local link=$1 target=$2 temporary="${1}.new"
    ln -sfn -- "$target" "$temporary"
    mv -Tf -- "$temporary" "$link"
}

install_client() {
    set -euo pipefail
    umask 077
    local root=/opt/clm-client sources unit
    if (($#)); then
        echo 'Run install-client.sh without arguments.' >&2
        exit 2
    fi
    if [[ $EUID != 0 ]]; then
        echo 'Run the installer as root.' >&2
        exit 1
    fi
    # shellcheck source=/dev/null
    . /etc/os-release
    if [[ $ID != debian || $VERSION_ID != 13 ]]; then
        echo 'This installer requires Debian 13.' >&2
        exit 1
    fi
    if [[ -L $root || (-e $root && $(stat -c %u -- "$root") != 0) ]]; then
        echo 'The application directory must be owned by root and may not be a symbolic link.' >&2
        exit 1
    fi

    sources=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates openssl logrotate

    install -d -m 0755 "$root" "$root/bin" "$root/venv"
    install -d -m 0711 "$root/run"
    install -d -m 0700 "$root/config"
    exec 9>"$root/run/install.lock"
    flock -x 9
    workspace=$(mktemp -d "$root/run/install.XXXXXXXX")
    trap 'rm -rf -- "$workspace"' EXIT

    install -d -m 0700 "$root/certificates" "$root/data" "$root/logs"
    keep_configuration 0600 /dev/null "$root/run/update.lock"
    exec 8<>"$root/run/update.lock"
    flock -x 8
    install_client_configuration "$sources" "$root/config"
    prepare_client_environment "$sources" "$root" "$workspace"
    for unit in clm-client-update.timer clm-client-update.service; do
        install -m 0644 "$sources/packaging/systemd/$unit" /etc/systemd/system/
    done
    install -m 0644 "$sources/packaging/logrotate/clm-client" /etc/logrotate.d/clm-client
    publish_application "$root/app" "$workspace"
    install -m 0755 -- "$workspace/bin/clm-client" "$root/bin/clm-client"
    switch_link /usr/local/bin/clm-client "$root/bin/clm-client"
    remove_legacy_layout "$root"
    echo 'Installed CLM Debian client. Configuration directory /opt/clm-client/config.'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    install_client "$@"
fi
