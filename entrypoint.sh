#!/bin/sh
set -eu

git clone -q -b "$GARDENLINUX_BRANCH" --single-branch https://github.com/gardenlinux/builder.git /gardenlinux-builder
git clone -q -b "$BUILDER_BRANCH" --single-branch https://github.com/gardenlinux/gardenlinux.git /gardenlinux

[ -z "$CREDENTIALS_KEY" ] || {
  CREDENTIALS_JSON_PATH="$(mktemp)"
  rm "$CREDENTIALS_JSON_PATH"
  printf '%s' "$CREDENTIALS_KEY" | gpg --batch --passphrase-fd 0 -qdo "$CREDENTIALS_JSON_PATH" "$CREDENTIALS_JSON_GPG_PATH"
}

[ -z "$CREDENTIALS_JSON_BASE64" ] || {
  printf '%s' "$CREDENTIALS_JSON_BASE64" | base64 -d > "$CREDENTIALS_JSON_PATH"
}

[ -z "$CREDENTIALS_JSON" ] || {
  printf '%s' "$CREDENTIALS_JSON" > "$CREDENTIALS_JSON_PATH"
}

SECRETS_SERVER_CACHE="$CREDENTIALS_JSON_PATH"
export SECRETS_SERVER_CACHE

cd /glci
exec "$@"
