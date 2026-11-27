#!/bin/bash
# Leave configuration already present on the host in place.

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
    keep_configuration 0600 /dev/null "$config/aws/config"
    keep_configuration 0644 "$sources/packaging/nginx/clm.conf" "$site"
}

install_client_configuration() {
    local sources=$1 config=$2
    install -d -m 0700 "$config/services.d"
    keep_configuration 0600 "$sources/examples/client/client.yaml" "$config/client.yaml"
    keep_configuration 0600 "$sources/examples/client/services.d/nginx.yaml" \
        "$config/services.d/nginx.yaml"
}
