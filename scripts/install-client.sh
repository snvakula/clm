#!/bin/bash
set -eu
exec "$(dirname -- "${BASH_SOURCE[0]}")/deploy.sh" client "$@"
