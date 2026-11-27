#!/usr/bin/env bash
set -euo pipefail
umask 077

install_files() {
    local sources=$1 root=$2 local_bin=$3 config hook link
    install -d -m 0755 -- "$root" "$root/bin"
    install -d -m 0700 -- "$root/config" "$root/config/hooks.d" "$root/run" "$root/data"
    install -d -m 0750 -- "$root/certificates"
    [[ ! -L $root/run/update.lock &&
        ( ! -e $root/run/update.lock || -f $root/run/update.lock ) ]]
    exec 9>>"$root/run/update.lock"
    flock -x 9
    config="$root/config/client.conf"
    hook="$root/config/hooks.d/restart-web-servers"
    if [[ ! -e $config && ! -L $config ]]; then
        install -m 0600 -- "$sources/examples/client-bash/client.conf" "$config"
    fi
    if [[ ! -e $hook && ! -L $hook ]]; then
        install -m 0700 -- "$sources/examples/client-bash/restart-web-servers" "$hook"
    fi
    install -m 0755 -- "$sources/src/clm_client_bash/clm-client-bash" "$root/bin/clm-client-bash"
    (umask 022; mkdir -p -- "$local_bin")
    link="$local_bin/clm-client-bash"
    if [[ ! -L $link || $(readlink -- "$link") != "$root/bin/clm-client-bash" ]]; then
        ln -sT -- "$root/bin/clm-client-bash" "$link"
    fi
    flock -u 9
    exec 9>&-
}

install_client() {
    local sources root unit
    if (( $# != 0 || EUID != 0 )); then
        printf '%s\n' 'Run install-client-bash.sh as root without arguments.' >&2
        exit 1
    fi

    sources=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
    root=/opt/clm-client-bash
    for tool in curl jq openssl unzip flock timeout install stat sync date base64 sha256sum; do
        command -v "$tool" >/dev/null || { printf 'Required tool is missing: %s\n' "$tool" >&2; exit 1; }
    done
    if [[ -L $root || ( -e $root && $(stat -c %u -- "$root") != 0 ) ]]; then
        printf '%s\n' 'The application directory must be owned by root.' >&2
        exit 1
    fi
    install_files "$sources" "$root" /usr/local/bin

    if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null; then
        install -d -m 0755 -- "$root/systemd"
        for unit in clm-client-bash-update.service clm-client-bash-update.timer; do
            install -m 0644 -- "$sources/packaging/systemd/$unit" "$root/systemd/$unit"
            systemctl link "$root/systemd/$unit"
        done
        systemctl daemon-reload
        systemctl enable --now clm-client-bash-update.timer
    fi
    printf '%s\n' 'Installed CLM Bash client. Edit /opt/clm-client-bash/config/client.conf.'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    install_client "$@"
fi
