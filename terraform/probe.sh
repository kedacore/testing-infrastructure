#!/usr/bin/env bash
# C211-I121896 terraform data.external wrapper: resource-creation-free cleanup-verify
# of the c211-i121663-rg A100 Container Apps residues (deletes only own c211-i121663* objects).
set +e
cd "$(dirname "$0")" || exit 1
python3 ./cleanup_a100.py
RC=$?
if [ $RC -ne 0 ]; then
  echo '{"verdict":"CLEANUP_PROCESS_ERROR"}'
fi
exit 0
