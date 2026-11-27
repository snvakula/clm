#!/bin/bash

keep_configuration() {
    local mode=$1 source=$2 target=$3
    if [[ ! -e $target && ! -L $target ]]; then
        install -m "$mode" -- "$source" "$target"
    fi
}

install_server_configuration() {
    local sources=$1 config=$2 site=$3 checksum
    install -d -m 0700 "$config/aws"
    keep_configuration 0600 "$sources/examples/server/clm.yaml" "$config/clm.yaml"
    if [[ -f $config/route53.yaml && ! -L $config/route53.yaml ]]; then
        checksum=$(sha256sum < "$config/route53.yaml")
        # Refresh the comments in the two unchanged defaults shipped previously.
        case ${checksum%% *} in
            b625eb7de5aeef9f6d6f3782bd9c5945449b7c294002654ac386f65206d8c66d|\
            ef5a1c1f4a3030eea0d42ef0dc8089fef4fc7f4fb3af9a26dbb2875d14ee34f4)
                install -m 0600 "$sources/packaging/route53.yaml" "$config/route53.yaml" ;;
        esac
    fi
    keep_configuration 0600 "$sources/packaging/route53.yaml" "$config/route53.yaml"
    keep_configuration 0600 "$sources/examples/server/credentials" "$config/aws/credentials"
    if [[ -f $config/aws/config && ! -L $config/aws/config && ! -s $config/aws/config ]]; then
        rm -- "$config/aws/config"
    fi
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

remove_legacy_layout() {
    local root=$1 entry name
    for entry in "$root/venv/"*; do
        name=${entry##*/}
        if [[ $name =~ ^[a-f0-9]{64}$ && ! -L $entry && -f $entry/pyvenv.cfg ]]; then
            rm -rf -- "$entry"
        fi
    done
    if [[ -L $root/acme/engine ]]; then
        rm -- "$root/acme/engine"
    fi
    if [[ -d $root/acme/engines && ! -L $root/acme/engines ]]; then
        for entry in "$root/acme/engines/"*; do
            name=${entry##*/}
            if [[ $name =~ ^[a-f0-9]{40}-[a-f0-9]{64}$ && ! -L $entry && -f $entry/acme.sh ]]; then
                rm -rf -- "$entry"
            fi
        done
        if [[ -z $(ls -A -- "$root/acme/engines") ]]; then
            rmdir -- "$root/acme/engines"
        fi
    fi
}

keep_file() {
    local mode=$1 group=$2 source=$3 target=$4
    if [[ ! -e $target && ! -L $target ]]; then
        install -m "$mode" -o root -g "$group" -- "$source" "$target"
    fi
}
switch_link() {
    local link=$1 target=$2 temporary="${1}.new"
    ln -sfn -- "$target" "$temporary"
    mv -Tf -- "$temporary" "$link"
}

install_server() {
    set -euo pipefail
    umask 077
    local root=/opt/clm sources environment unit
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

    getent group clm-api >/dev/null || groupadd --system clm-api
    id clm-api >/dev/null 2>&1 || useradd --system --gid clm-api --home-dir "$root" \
        --no-create-home --shell /usr/sbin/nologin clm-api
    if [[ $(id -u clm-api) == 0 || $(id -gn clm-api) != clm-api ]]; then
        echo 'The clm-api identity must be unprivileged and use the clm-api group.' >&2
        exit 1
    fi
    install -d -m 3770 -o root -g clm-api "$root/data"
    install -d -m 2750 -o root -g clm-api "$root/data/certificates" "$root/logs"
    install -d -m 0700 "$root/acme" "$root/acme/instances"
    install_server_configuration "$sources" "$root/config" /etc/nginx/sites-available/clm
    keep_file 0640 clm-api /dev/null "$root/run/publication.lock"
    keep_file 0600 root /dev/null "$root/run/operations.lock"
    exec 8<>"$root/run/operations.lock"
    flock -x 8
    if [[ ! -e $root/logs/clmd.log ]]; then
        install -m 0600 -o clm-api -g clm-api /dev/null "$root/logs/clmd.log"
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
    keep_file 0660 clm-api /dev/null "$root/data/clm.db"
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

    for unit in clmd.socket clmd.service clm-renew.timer clm-renew.service; do
        install -m 0644 "$sources/packaging/systemd/$unit" /etc/systemd/system/
    done
    install -m 0644 "$sources/packaging/logrotate/clm" /etc/logrotate.d/clm
    install -d -m 0700 "$root/acme/dnsapi"
    install -m 0700 -- "$workspace/acme/acme.sh" "$workspace/acme/recover-dns.sh" "$root/acme/"
    install -m 0600 -- "$workspace/acme/LICENSE.md" "$root/acme/"
    install -m 0600 -- "$workspace/acme/dnsapi/dns_aws.sh" "$root/acme/dnsapi/"
    publish_application "$root/app" "$workspace"
    install -m 0755 -- "$workspace/bin/clm" "$workspace/bin/clmd" "$root/bin/"
    switch_link /usr/local/bin/clm "$root/bin/clm"
    switch_link /usr/local/bin/clmd "$root/bin/clmd"
    remove_legacy_layout "$root"
    echo 'Installed CLM server. Configuration directory /opt/clm/config.'
    echo 'Nginx site configuration is at /etc/nginx/sites-available/clm.'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    install_server "$@"
fi
