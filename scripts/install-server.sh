#!/bin/bash

keep_configuration() {
    local mode=$1 source=$2 target=$3
    if [[ ! -e $target && ! -L $target ]]; then
        install -m "$mode" -- "$source" "$target"
    fi
}

install_server_configuration() {
    local sources=$1 config=$2 site=$3
    install -d -m 0700 "$config/aws"
    keep_configuration 0600 "$sources/examples/server/clm.yaml" "$config/clm.yaml"
    keep_configuration 0600 "$sources/packaging/route53.yaml" "$config/route53.yaml"
    keep_configuration 0600 "$sources/examples/server/credentials" "$config/aws/credentials"
    keep_configuration 0644 "$sources/packaging/nginx/clm.conf" "$site"
}

stage_server_files() {
    local sources=$1 workspace=$2 interpreter=$3
    "$interpreter" - "$sources" "$workspace" "$interpreter" <<'PYTHON'
import shutil
import sys
import tomllib
from pathlib import Path

source, workspace, interpreter = map(Path, sys.argv[1:])
package = source / "src/clm_server"
metadata = tomllib.loads((source / "pyproject.toml").read_text())["project"]
app = workspace / "app"
app.mkdir()
for module in package.glob("*.py"):
    shutil.copyfile(module, app / module.name)
with (app / "__init__.py").open("a") as module:
    module.write(f"\n__version__ = {metadata['version']!r}\n")

commands = workspace / "bin"
commands.mkdir()
for name in ('clm', 'clmd'):
    entry, function = metadata["scripts"][name].split(":")
    module = entry.split(".", 1)[1]
    (commands / name).write_text(
        f"#!{interpreter} -B\n"
        f"from app.{module} import {function}\n"
        f"raise SystemExit({function}())\n"
    )
PYTHON
}

prepare_server_environment() {
    local sources=$1 root=$2 workspace=$3
    local environment="$root/venv" site_packages
    if [[ ! -f $environment/pyvenv.cfg ]]; then
        python3 -m venv "$environment"
    fi
    "$environment/bin/python" -m pip --disable-pip-version-check install \
        --require-hashes -r "$sources/packaging/requirements/server.txt"
    "$environment/bin/python" -m pip check
    stage_server_files "$sources" "$workspace" "$environment/bin/python"
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

keep_file() {
    local mode=$1 group=$2 source=$3 target=$4
    if [[ ! -e $target && ! -L $target ]]; then
        install -m "$mode" -o root -g "$group" -- "$source" "$target"
    fi
}
install_cli_link() {
    local link=$1 destination=$2
    if [[ -L $link && $(readlink -- "$link") == "$destination" ]]; then
        return
    fi
    ln -sT -- "$destination" "$link"
}

configure_server_identity() {
    local root=$1
    if ! getent group clm >/dev/null; then
        groupadd --system clm
    fi
    if ! id clm >/dev/null 2>&1; then
        useradd --system --gid clm --home-dir "$root" \
            --no-create-home --shell /usr/sbin/nologin clm
    fi
    if [[ $(id -u clm) == 0 || $(id -gn clm) != clm ]]; then
        echo 'The clm identity must be unprivileged and use the clm group.' >&2
        return 1
    fi
}

install_server_units() {
    local sources=$1 root=$2 system_units=$3 unit
    install -d -m 0755 "$root/systemd"
    for unit in clmd.socket clmd.service clm-renew.timer clm-renew.service; do
        install -m 0644 -- "$sources/packaging/systemd/$unit" "$root/systemd/$unit"
        ln -sfn -- "$root/systemd/$unit" "$system_units/$unit.new"
        mv -Tf -- "$system_units/$unit.new" "$system_units/$unit"
    done
    systemctl daemon-reload
    systemctl enable --now clm-renew.timer
}

install_server() {
    set -euo pipefail
    umask 077
    local root=/opt/clm sources environment
    if (($#)); then
        echo 'Run install-server.sh without arguments.' >&2
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
        python3 python3-venv ca-certificates openssl logrotate nginx curl patch

    install -d -m 0755 "$root" "$root/bin" "$root/venv"
    install -d -m 0711 "$root/run"
    install -d -m 0700 "$root/config"
    exec 9>"$root/run/install.lock"
    flock -x 9
    workspace=$(mktemp -d "$root/run/install.XXXXXXXX")
    trap 'rm -rf -- "$workspace"' EXIT

    configure_server_identity "$root"
    install -d -m 3770 -o root -g clm "$root/data"
    install -d -m 2750 -o root -g clm "$root/data/certificates" "$root/logs"
    install -d -m 0700 "$root/acme" "$root/acme/instances"
    install_server_configuration "$sources" "$root/config" /etc/nginx/sites-available/clm
    keep_file 0640 clm /dev/null "$root/run/publication.lock"
    keep_file 0600 root /dev/null "$root/run/operations.lock"
    exec 8<>"$root/run/operations.lock"
    flock -x 8
    if [[ ! -e $root/logs/clmd.log ]]; then
        install -m 0600 -o clm -g clm /dev/null "$root/logs/clmd.log"
    fi

    local revision=5567d7fe7298fd37aca53194885ba444c6678669
    mkdir -- "$workspace/acme"
    curl --disable --fail --silent --show-error --location --proto '=https' \
        --proto-redir '=https' --max-time 120 \
        "https://codeload.github.com/acmesh-official/acme.sh/tar.gz/$revision" \
        --output "$workspace/acme.tar.gz"
    printf '%s  %s\n' \
        22ca219a96d643910f79d2888d735a444672e5bc0179d3eb6142f0642a9682dc \
        "$workspace/acme.tar.gz" | sha256sum --check --status
    tar -xf "$workspace/acme.tar.gz" -C "$workspace/acme" --strip-components=1 \
        "acme.sh-$revision/acme.sh" "acme.sh-$revision/dnsapi/dns_aws.sh" "acme.sh-$revision/LICENSE.md"
    patch --batch --fuzz=0 -d "$workspace/acme" -p1 < "$sources/packaging/acme/dns-aws.patch"
    install -m 0700 "$sources/packaging/acme/recover-dns.sh" "$workspace/acme/recover-dns.sh"
    "$workspace/acme/acme.sh" --version >/dev/null

    prepare_server_environment "$sources" "$root" "$workspace"
    environment="$root/venv"
    # SQLite derives its WAL and shared-memory permissions from the database mode.
    keep_file 0660 clm /dev/null "$root/data/clm.db"
    PYTHONPATH="$workspace" "$environment/bin/python" -B - "$root/data/clm.db" <<'PY'
import sys
from pathlib import Path
from app.database import Database
database = Database(Path(sys.argv[1]))
database.initialise()
with database.connect() as connection:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("CLM database integrity check failed")
PY

    install -m 0644 "$sources/packaging/logrotate/clm" /etc/logrotate.d/clm
    install -d -m 0700 "$root/acme/dnsapi"
    install -m 0700 -- "$workspace/acme/acme.sh" "$workspace/acme/recover-dns.sh" "$root/acme/"
    install -m 0600 -- "$workspace/acme/LICENSE.md" "$root/acme/"
    install -m 0600 -- "$workspace/acme/dnsapi/dns_aws.sh" "$root/acme/dnsapi/"
    publish_application "$root/app" "$workspace"
    install -m 0755 -- "$workspace/bin/clm" "$workspace/bin/clmd" "$root/bin/"
    (umask 022; mkdir -p /usr/local/bin)
    install_cli_link /usr/local/bin/clm "$root/bin/clm"
    install_cli_link /usr/local/bin/clmd "$root/bin/clmd"
    flock -u 8
    install_server_units "$sources" "$root" /etc/systemd/system
    echo 'Installed CLM server. Configuration directory /opt/clm/config.'
    echo 'Nginx site configuration is at /etc/nginx/sites-available/clm.'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    install_server "$@"
fi
