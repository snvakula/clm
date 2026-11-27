#!/bin/bash
set -euo pipefail
umask 077

product=$1
sources=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $EUID != 0 ]]; then
    echo 'Run the installer as root.' >&2
    exit 1
fi
if [[ $# != 1 || ($product != server && $product != client) ]]; then
    echo 'Run install-server.sh or install-client.sh without arguments.' >&2
    exit 2
fi
# shellcheck source=/dev/null
. /etc/os-release
if [[ $ID != debian || $VERSION_ID != 13 ]]; then
    echo 'This installer requires Debian 13.' >&2
    exit 1
fi

if [[ $product == server ]]; then
    root=/opt/clm
    program=clm
    packages=(nginx curl patch)
else
    root=/opt/clm-client
    program=clm-client
    packages=()
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends     python3 python3-venv ca-certificates openssl logrotate "${packages[@]}"

if [[ -L $root || (-e $root && $(stat -c %u -- "$root") != 0) ]]; then
    echo 'The application directory must be owned by root and may not be a symbolic link.' >&2
    exit 1
fi
install -d -m 0755 "$root" "$root/bin" "$root/venv"
install -d -m 0711 "$root/run"
install -d -m 0700 "$root/config"
exec 9>"$root/run/install.lock"
flock -x 9
workspace=$(mktemp -d "$root/run/install.XXXXXXXX")
trap 'rm -rf -- "$workspace"' EXIT
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

# shellcheck source=/dev/null
. "$sources/scripts/install-configuration.sh"
# shellcheck source=/dev/null
. "$sources/scripts/install-layout.sh"

if [[ $product == server ]]; then
    getent group clm-api >/dev/null || groupadd --system clm-api
    id clm-api >/dev/null 2>&1 || useradd --system --gid clm-api --home-dir "$root"         --no-create-home --shell /usr/sbin/nologin clm-api
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

    revision=5567d7fe7298fd37aca53194885ba444c6678669
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
else
    install -d -m 0700 "$root/certificates" "$root/data" "$root/logs"
    keep_file 0600 root /dev/null "$root/run/update.lock"
    exec 8<>"$root/run/update.lock"
    flock -x 8
    install_client_configuration "$sources" "$root/config"
fi

stage_application "$sources" "$product" "$root" "$workspace"
environment="$root/venv"

if [[ $product == server ]]; then
    # SQLite derives its WAL and shared-memory permissions from the database mode.
    keep_file 0660 clm-api /dev/null "$root/data/clm.db"
    PYTHONPATH="$workspace/application" "$environment/bin/python" - "$root/data/clm.db" <<'PY'
import sys
from pathlib import Path
from clm_server.database import Database
database = Database(Path(sys.argv[1]))
database.initialise()
with database.connect() as connection:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("CLM database integrity check failed")
PY
    units=(clmd.socket clmd.service clm-renew.timer clm-renew.service)
else
    units=(clm-client-update.timer clm-client-update.service)
fi
for unit in "${units[@]}"; do
    install -m 0644 "$sources/packaging/systemd/$unit" /etc/systemd/system/
done
install -m 0644 "$sources/packaging/logrotate/$program" /etc/logrotate.d/"$program"
if [[ $product == server ]]; then
    install -d -m 0700 "$root/acme/dnsapi"
    install -m 0700 -- "$workspace/acme/acme.sh" "$workspace/acme/recover-dns.sh" "$root/acme/"
    install -m 0600 -- "$workspace/acme/LICENSE.md" "$root/acme/"
    install -m 0600 -- "$workspace/acme/dnsapi/dns_aws.sh" "$root/acme/dnsapi/"
fi
publish_application "$root/app" "$workspace"
switch_link "$root/bin/$program" "$root/app/bin/$program"
switch_link "/usr/local/bin/$program" "$root/bin/$program"
if [[ $product == server ]]; then
    switch_link "$root/bin/clmd" "$root/app/bin/clmd"
    switch_link /usr/local/bin/clmd "$root/bin/clmd"
fi
remove_legacy_layout "$product" "$root"
printf 'Installed %s under %s. Configuration directory %s/config.\n' "$program" "$root" "$root"
if [[ $product == server ]]; then
    echo 'Nginx site configuration is at /etc/nginx/sites-available/clm.'
fi
