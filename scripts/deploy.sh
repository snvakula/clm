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
install -d -m 0755 "$root" "$root/bin" "$root/app" "$root/app/releases" "$root/venv"
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

if [[ $product == server ]]; then
    getent group clm-api >/dev/null || groupadd --system clm-api
    id clm-api >/dev/null 2>&1 || useradd --system --gid clm-api --home-dir "$root"         --no-create-home --shell /usr/sbin/nologin clm-api
    install -d -m 3770 -o root -g clm-api "$root/data"
    install -d -m 2750 -o root -g clm-api "$root/data/certificates" "$root/logs"
    install -d -m 0700 "$root/config/aws" "$root/acme" "$root/acme/instances" "$root/acme/engines"
    keep_file 0600 root "$sources/examples/server/clm.yaml" "$root/config/clm.yaml"
    if [[ ! -e $root/config/route53.yaml ]]; then
        printf 'accounts: {}\nchallenge_aliases: {}\n' > "$workspace/route53.yaml"
        keep_file 0600 root "$workspace/route53.yaml" "$root/config/route53.yaml"
    fi
    keep_file 0600 root /dev/null "$root/config/aws/credentials"
    keep_file 0600 root /dev/null "$root/config/aws/config"
    install -m 0644 "$sources/packaging/nginx/clm.conf" "$root/config/nginx.conf.example"
    keep_file 0640 clm-api /dev/null "$root/run/publication.lock"
    keep_file 0600 root /dev/null "$root/run/operations.lock"
    exec 8<>"$root/run/operations.lock"
    flock -x 8
    if [[ ! -e $root/logs/clmd.log ]]; then
        install -m 0600 -o clm-api -g clm-api /dev/null "$root/logs/clmd.log"
    fi

    revision=5567d7fe7298fd37aca53194885ba444c6678669
    engine_id=$(sha256sum "$sources/packaging/acme/dns-aws.patch"         "$sources/packaging/acme/recover-dns.sh" | cut -d ' ' -f1 | sha256sum | cut -d ' ' -f1)
    engine="$root/acme/engines/${revision}-${engine_id}"
    if [[ ! -d $engine ]]; then
        curl --disable --fail --silent --show-error --location --proto '=https'             --proto-redir '=https' --max-time 120             "https://codeload.github.com/acmesh-official/acme.sh/tar.gz/$revision"             --output "$workspace/acme.tar.gz"
        printf '%s  %s\n'             22ca219a96d643910f79d2888d735a444672e5bc0179d3eb6142f0642a9682dc             "$workspace/acme.tar.gz" | sha256sum --check --status
        mkdir "$workspace/engine"
        tar -xf "$workspace/acme.tar.gz" -C "$workspace/engine" --strip-components=1             "acme.sh-$revision/acme.sh" "acme.sh-$revision/dnsapi/dns_aws.sh"
        patch --batch --fuzz=0 -d "$workspace/engine" -p1 < "$sources/packaging/acme/dns-aws.patch"
        install -m 0700 "$sources/packaging/acme/recover-dns.sh" "$workspace/engine/recover-dns.sh"
        "$workspace/engine/acme.sh" --version >/dev/null
        mv -T -- "$workspace/engine" "$engine"
    fi
else
    install -d -m 0700 "$root/config/services.d" "$root/certificates" "$root/data" "$root/logs"
    keep_file 0600 root "$sources/examples/client/client.yaml" "$root/config/client.yaml"
    keep_file 0600 root "$sources/examples/client/services.d/nginx.yaml" "$root/config/services.d/nginx.yaml"
fi

release=$(
    cd -- "$sources"
    {
        find src -type f -name '*.py' -print0 | sort -z | xargs -0 sha256sum
        sha256sum pyproject.toml "packaging/requirements/$product.txt"
        python3 --version
    } | sha256sum | cut -d ' ' -f1
)
environment="$root/venv/$release"
application="$root/app/releases/$release"
if [[ ! -f $application/.ready ]]; then
    rm -rf -- "$environment" "$application"
    mkdir -p "$workspace/source/src" "$workspace/application"
    cp -- "$sources/pyproject.toml" "$workspace/source/"
    (
        cd -- "$sources"
        find src -type f -name '*.py' -exec cp --parents -t "$workspace/source" -- {} +
    )
    python3 -m venv "$environment"
    "$environment/bin/python" -m pip --disable-pip-version-check install         -r "$sources/packaging/requirements/$product.txt" 'setuptools==80.9.0'
    "$environment/bin/python" -m pip --disable-pip-version-check install --no-deps         --no-build-isolation "$workspace/source"
    "$environment/bin/python" -m pip check
    ln -s -- "$environment/bin/$program" "$workspace/application/$program"
    if [[ $product == server ]]; then
        ln -s -- "$environment/bin/clmd" "$workspace/application/clmd"
    fi
    chmod 0755 "$workspace/application"
    # The API identity needs read/execute access to the installed code.
    chmod -R a+rX -- "$environment"
    chmod 0755 "$environment"
    touch "$workspace/application/.ready"
    mv -T -- "$workspace/application" "$application"
fi

previous=$(readlink "$root/app/current" || :)
if [[ $product == server ]]; then
    previous_engine=$(readlink "$root/acme/engine" || :)
    # SQLite derives its WAL and shared-memory permissions from the database mode.
    keep_file 0660 clm-api /dev/null "$root/data/clm.db"
    "$environment/bin/python" - "$root/data/clm.db" <<'PY'
import sys
from pathlib import Path
from clm_server.database import Database
database = Database(Path(sys.argv[1]))
database.initialise()
with database.connect() as connection:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("CLM database integrity check failed")
PY
    nginx -t
    units=(clmd.socket clmd.service clm-renew.timer clm-renew.service)
else
    units=(clm-client-update.timer clm-client-update.service)
fi
for unit in "${units[@]}"; do
    install -m 0644 "$sources/packaging/systemd/$unit" /etc/systemd/system/
done
install -m 0644 "$sources/packaging/logrotate/$program" /etc/logrotate.d/"$program"
switch_link "$root/bin/$program" "$root/app/current/$program"
switch_link "/usr/local/bin/$program" "$root/bin/$program"
if [[ $product == server ]]; then
    switch_link "$root/bin/clmd" "$root/app/current/clmd"
    switch_link /usr/local/bin/clmd "$root/bin/clmd"
    switch_link "$root/acme/engine" "$engine"
fi
switch_link "$root/app/current" "releases/$release"

activate() {
    systemctl daemon-reload || return
    if [[ $product == server ]]; then
        systemctl enable --now clmd.socket clm-renew.timer || return
        systemctl try-restart clmd.service || return
    else
        systemctl enable --now clm-client-update.timer || return
    fi
}
if ! activate; then
    if [[ -n $previous ]]; then
        switch_link "$root/app/current" "$previous"
        if [[ $product == server && -n $previous_engine ]]; then
            switch_link "$root/acme/engine" "$previous_engine"
        fi
        activate || :
    fi
    echo 'Failed to activate the systemd units.' >&2
    exit 1
fi
echo "Installed $program under $root."
