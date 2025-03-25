#!/bin/sh
set -eu

git clone -q https://github.com/gardenlinux/builder.git /gardenlinux-builder
git clone -q https://github.com/gardenlinux/gardenlinux.git /gardenlinux

[ -z "$CREDENTIALS_KEY" ] || {
  CREDENTIALS_JSON_PATH="$(mktemp)"
  rm "$CREDENTIALS_JSON_PATH"
  printf '%s' "$CREDENTIALS_KEY" | gpg --batch --passphrase-fd 0 -qdo "$CREDENTIALS_JSON_PATH" "$CREDENTIALS_JSON_GPG_PATH"
}

SECRETS_SERVER_CACHE="$CREDENTIALS_JSON_PATH"
export SECRETS_SERVER_CACHE

cd /glci
exec "$@"
