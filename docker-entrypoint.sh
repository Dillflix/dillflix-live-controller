#!/bin/sh
set -eu
# Existing named volumes predate this user's persistent ADB home directory.
mkdir -p /data/adb
exec "$@"
