#!/bin/bash
set -euo pipefail
umask 077

product=$1
sources=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
shift
config_source=
server_url=
token_file=
usage() {
    if [[ $product == server ]]; then
        echo 'Usage: install-server.sh [--config-dir DIRECTORY]'
        echo 'Supply clm.yaml, route53.yaml and aws/credentials. Include nginx.conf to enable the HTTPS site.'
    else
        echo 'Usage: install-client.sh [--server HTTPS_URL --credentials-file TOKEN_FILE]'
    fi
}
while (( $# )); do
    case $1 in
        --help|-h) usage; exit 0 ;;
        --config-dir)
            [[ $product == server && $# -ge 2 && -n $2 ]] || { usage >&2; exit 2; }
            config_source=$2 ;;
        --server)
            [[ $product == client && $# -ge 2 && -n $2 ]] || { usage >&2; exit 2; }
            server_url=$2 ;;
        --credentials-file)
            [[ $product == client && $# -ge 2 && -n $2 ]] || { usage >&2; exit 2; }
            token_file=$2 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift 2
done
if [[ (-n $server_url && -z $token_file) || (-z $server_url && -n $token_file) ]]; then
    usage >&2
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

if [[ $product == server ]]; then
    root=/opt/clm
    program=clm
    packages=(nginx curl patch)
else
    root=/opt/clm-client
    program=clm-client
    packages=()
fi
if [[ -L $root || (-e $root && $(stat -c %u -- "$root") != 0) ]]; then
    echo 'The application directory must be owned by root and may not be a symbolic link.' >&2
    exit 1
fi
if [[ $product == server ]]; then
    input=${config_source:-$root/config}
    for file in clm.yaml route53.yaml aws/credentials; do
        if [[ ! -f $input/$file ]]; then
            printf 'Missing configuration file %s. Supply the server files with --config-dir.\n' \
                "$input/$file" >&2
            exit 1
        fi
    done
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    python3 python3-venv ca-certificates openssl logrotate "${packages[@]}"

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
    if [[ $(id -u clm-api) == 0 || $(id -gn clm-api) != clm-api ]]; then
        echo 'The clm-api identity must be unprivileged and use the clm-api group.' >&2
        exit 1
    fi
    install -d -m 3770 -o root -g clm-api "$root/data"
    install -d -m 2750 -o root -g clm-api "$root/data/certificates" "$root/logs"
    install -d -m 0700 "$root/config/aws" "$root/acme" "$root/acme/instances" "$root/acme/engines"
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
        tar -xf "$workspace/acme.tar.gz" -C "$workspace/engine" --strip-components=1             "acme.sh-$revision/acme.sh" "acme.sh-$revision/dnsapi/dns_aws.sh" "acme.sh-$revision/LICENSE.md"
        patch --batch --fuzz=0 -d "$workspace/engine" -p1 < "$sources/packaging/acme/dns-aws.patch"
        install -m 0700 "$sources/packaging/acme/recover-dns.sh" "$workspace/engine/recover-dns.sh"
        "$workspace/engine/acme.sh" --version >/dev/null
        mv -T -- "$workspace/engine" "$engine"
    fi
else
    install -d -m 0700 "$root/config/services.d" "$root/certificates" "$root/data" "$root/logs"
    keep_file 0600 root /dev/null "$root/run/update.lock"
    exec 8<>"$root/run/update.lock"
    flock -x 8
    keep_file 0600 root "$sources/examples/client/client.yaml" "$root/config/client.yaml"
    keep_file 0600 root "$sources/examples/client/services.d/nginx.yaml" "$root/config/services.d/nginx.yaml"
fi

release=$(
    cd -- "$sources"
    {
        find src -type f -name '*.py' -print0 | sort -z | xargs -0 sha256sum
        sha256sum pyproject.toml "packaging/requirements/$product.txt" packaging/requirements/build.txt
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
    "$environment/bin/python" -m pip --disable-pip-version-check install \
        --require-hashes -r "$sources/packaging/requirements/$product.txt" \
        -r "$sources/packaging/requirements/build.txt"
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
    # shellcheck source=/dev/null
    . "$sources/scripts/server-configuration.sh"
    candidate_config="$root/config"
    if [[ -n $config_source ]]; then
        candidate_config="$workspace/config"
        mkdir -p -- "$candidate_config/aws"
        for file in clm.yaml route53.yaml aws/credentials aws/config; do
            if [[ -e $root/config/$file || -L $root/config/$file ]]; then
                cp -P --preserve=mode -- "$root/config/$file" "$candidate_config/$file"
            fi
        done
        server_configuration "$config_source" "$candidate_config" \
            "$sources/examples/server/clm.yaml" >/dev/null
    fi
    # SQLite derives its WAL and shared-memory permissions from the database mode.
    keep_file 0660 clm-api /dev/null "$root/data/clm.db"
    "$environment/bin/python" - "$root" "$candidate_config" <<'PY'
import sys
from pathlib import Path
from clm_server.database import Database
from clm_server.route53 import Route53
from clm_server.service import account_email

root = Path(sys.argv[1])
config = Path(sys.argv[2])
try:
    account_email(config / "clm.yaml")
    route = Route53(config)
    for account in route.accounts:
        route.credentials(account)
except (OSError, ValueError) as error:
    raise SystemExit(f"Could not validate CLM configuration: {error}") from None
database = Database(root / "data/clm.db")
database.initialise()
with database.connect() as connection:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("CLM database integrity check failed")
PY
    if [[ -n $config_source ]]; then
        server_configuration "$candidate_config" "$root/config" "$sources/examples/server/clm.yaml"
    fi
    site=/etc/nginx/sites-available/clm
    enabled_site=/etc/nginx/sites-enabled/clm
    added_site=false
    enabled_new_site=false
    if [[ -n $config_source && -f $config_source/nginx.conf ]]; then
        if [[ ! -e $site && ! -L $site ]]; then
            install -m 0644 -- "$config_source/nginx.conf" "$site"
            added_site=true
        elif ! cmp -s -- "$config_source/nginx.conf" "$site"; then
            printf 'Using existing Nginx site %s\n' "$site"
        fi
        if [[ ! -e $enabled_site && ! -L $enabled_site ]]; then
            ln -s -- "$site" "$enabled_site"
            enabled_new_site=true
        fi
    fi
    if ! nginx -t; then
        if "$enabled_new_site"; then rm -- "$enabled_site"; fi
        if "$added_site"; then rm -- "$site"; fi
        exit 1
    fi
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
        if "$enabled_new_site"; then
            systemctl start nginx.service || return
            systemctl reload nginx.service || return
        fi
    else
        systemctl enable --now clm-client-update.timer || return
    fi
}
if ! activate; then
    if [[ $product == server ]]; then
        if "$enabled_new_site"; then rm -- "$enabled_site"; fi
        if "$added_site"; then rm -- "$site"; fi
        enabled_new_site=false
    fi
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
if [[ -n $previous && $previous != "releases/$release" ]]; then
    switch_link "$root/app/previous" "$previous"
fi
retained=$(readlink "$root/app/previous" || :)
for candidate in "$root/app/releases/"*; do
    name=${candidate##*/}
    if [[ $name =~ ^[a-f0-9]{64}$ && $name != "$release" && releases/$name != "$retained" ]]; then
        rm -rf -- "$candidate" "$root/venv/$name"
    fi
done
if [[ $product == client && -n $server_url ]]; then
    flock -u 8
    if [[ ! -e $root/config/credentials && ! -L $root/config/credentials ]]; then
        "$root/bin/clm-client" init "$server_url" --credentials-file "$token_file"
    fi
fi
printf 'Installed %s. Active configuration directory: %s/config\n' "$program" "$root"
