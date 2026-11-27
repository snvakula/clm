#!/bin/bash

stage_application() {
    local sources=$1 product=$2 root=$3 workspace=$4
    local environment="$root/venv" site_packages
    if [[ ! -f $environment/pyvenv.cfg ]]; then
        python3 -m venv "$environment"
    fi
    "$environment/bin/python" -m pip --disable-pip-version-check install \
        --require-hashes -r "$sources/packaging/requirements/$product.txt"
    "$environment/bin/python" "$sources/scripts/stage-application.py" \
        "$sources" "$product" "$workspace" "$environment/bin/python"
    "$environment/bin/python" -m pip check

    site_packages=$("$environment/bin/python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
    printf '%s\n' "$root/app" > "$workspace/clm-application.pth"
    install -m 0644 -- "$workspace/clm-application.pth" "$site_packages/clm-application.pth"
    # The unprivileged API identity needs read and execute access.
    chmod -R a+rX -- "$environment" "$workspace/application"
}

publish_application() {
    local target=$1 workspace=$2
    if [[ -e $target || -L $target ]]; then
        mv -T -- "$target" "$workspace/old-application"
    fi
    if ! mv -T -- "$workspace/application" "$target"; then
        if [[ -e $workspace/old-application || -L $workspace/old-application ]]; then
            mv -T -- "$workspace/old-application" "$target"
        fi
        return 1
    fi
}

remove_legacy_layout() {
    local product=$1 root=$2 entry name
    for entry in "$root/venv/"*; do
        name=${entry##*/}
        if [[ $name =~ ^[a-f0-9]{64}$ && ! -L $entry && -f $entry/pyvenv.cfg ]]; then
            rm -rf -- "$entry"
        fi
    done
    if [[ $product == server ]]; then
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
    fi
}
