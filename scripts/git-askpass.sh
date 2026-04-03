#!/usr/bin/env sh
set -eu

# Git calls this program with a prompt like:
# - "Username for 'https://github.com': "
# - "Password for 'https://x-access-token@github.com': "
#
# We return a fixed username and read the PAT from env.

case "${1:-}" in
  *Username*)
    printf '%s\n' "x-access-token"
    ;;
  *Password*)
    printf '%s\n' "${GITHUB_TOKEN:?GITHUB_TOKEN is not set}"
    ;;
  *)
    # Default to token to avoid hanging on unexpected prompts.
    printf '%s\n' "${GITHUB_TOKEN:?GITHUB_TOKEN is not set}"
    ;;
esac

