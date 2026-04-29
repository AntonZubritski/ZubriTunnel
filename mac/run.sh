#!/usr/bin/env bash
# Mac launcher — запуск из Terminal
set -e
cd "$(dirname "$0")"
go run . "$@"
