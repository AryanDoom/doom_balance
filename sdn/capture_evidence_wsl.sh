#!/usr/bin/env bash

set -e

windows_repo='/mnt/c/Code/balance_this_load'
wsl_repo='/home/doom/balance_this_load'

if [ ! -d "$windows_repo" ]; then
    echo "could not find the Windows project at $windows_repo"
    exit 1
fi

mkdir -p "$wsl_repo"

# syncing the Windows repo first so Mininet tests the same code as the repo on GitHub
cp "$windows_repo/Cargo.toml" "$wsl_repo/"
if [ -f "$windows_repo/Cargo.lock" ]; then
    cp "$windows_repo/Cargo.lock" "$wsl_repo/"
fi
if [ -f "$windows_repo/config.toml" ]; then
    cp "$windows_repo/config.toml" "$wsl_repo/"
fi
cp -r "$windows_repo/src" "$wsl_repo/"
cp -r "$windows_repo/tests" "$wsl_repo/"
cp -r "$windows_repo/sdn" "$wsl_repo/"

cd "$wsl_repo"
cargo build --release
python3 sdn/capture_evidence.py
