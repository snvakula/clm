#!/bin/bash
# Install operator-supplied configuration while preserving configured hosts.

server_configuration() {
    local source=$1 target=$2 default_email=$3
    local name current initial
    for name in clm.yaml route53.yaml aws/credentials; do
        current="$target/$name"
        if [[ -n $source ]]; then
            if [[ -L $current ]]; then
                printf 'Using existing configuration %s\n' "$current"
                continue
            fi
            initial=false
            case $name in
                clm.yaml)
                    if cmp -s -- "$current" "$default_email"; then initial=true; fi ;;
                route53.yaml)
                    if cmp -s -- "$current" <(printf 'accounts: {}\nchallenge_aliases: {}\n'); then
                        initial=true
                    fi ;;
            esac
            if [[ ! -s $current ]] || "$initial"; then
                install -m 0600 -- "$source/$name" "$current"
            elif ! cmp -s -- "$current" "$source/$name"; then
                printf 'Using existing configuration %s\n' "$current"
            fi
        fi
    done
    if [[ -n $source && -f $source/aws/config && ! -s $target/aws/config &&
          ! -L $target/aws/config ]]; then
        install -m 0600 -- "$source/aws/config" "$target/aws/config"
    fi
}
