#!/bin/bash
# Leave configuration already present on the host in place.

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
            b625eb7de5aeef9f6d6f3782bd9c5945449b7c294002654ac386f65206d8c66d|            ef5a1c1f4a3030eea0d42ef0dc8089fef4fc7f4fb3af9a26dbb2875d14ee34f4)
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

install_client_configuration() {
    local sources=$1 config=$2
    install -d -m 0700 "$config/services.d"
    keep_configuration 0600 "$sources/examples/client/client.yaml" "$config/client.yaml"
    keep_configuration 0600 "$sources/examples/client/services.d/nginx.yaml" \
        "$config/services.d/nginx.yaml"
}
