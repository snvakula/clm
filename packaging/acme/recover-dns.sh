#!/bin/sh
# Load the pinned acme.sh signing functions without running an ACME operation.
set -eu
clm_engine_directory=$1
clm_noop() { return 0; }
set -- clm_noop
set +eu
# shellcheck source=/dev/null
. "$clm_engine_directory/acme.sh"
# shellcheck source=/dev/null
. "$clm_engine_directory/dnsapi/dns_aws.sh"
dns_aws_clm_recover
