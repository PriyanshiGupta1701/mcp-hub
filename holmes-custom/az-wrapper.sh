#!/bin/bash
# Wrapper around the real Azure CLI binary. Guarantees Holmes can never hang
# indefinitely on an `az` call, regardless of what the LLM decides to pass.
#
# 1. 'az webapp log tail' without --timeout streams forever — auto-inject
#    a bounded --timeout if the caller forgot one.
# 2. Every az invocation gets a hard outer kill switch as a safety net, in
#    case some other subcommand blocks unexpectedly.

REAL_AZ="/usr/bin/az.real"
ARGS="$*"

if [[ "$ARGS" == *"log tail"* ]] && [[ "$ARGS" != *"--timeout"* ]]; then
    echo "[az-wrapper] 'log tail' called without --timeout — auto-adding --timeout 15" >&2
    exec timeout 30 "$REAL_AZ" "$@" --timeout 15
fi

exec timeout 90 "$REAL_AZ" "$@"
