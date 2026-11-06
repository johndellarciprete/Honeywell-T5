#!/usr/bin/env bash
# Windows workaround for `homey app validate` / `homey app run`.
#
# The Homey CLI cross-compiles the Python dependencies inside Docker (Linux),
# producing venvs under python_packages/<arch>/.venv. Those venvs contain
# absolute symlinks (bin/python -> /python/.../python3.13, lib64 -> lib) that do
# not exist on Windows. The CLI's packaging step does a recursive `fs.cp` of the
# whole venv and throws on the dangling symlinks:
#
#   × Error while collecting cross-compiled virtual environment for arm64.
#
# Only python_packages/<arch>/lib is actually shipped, so these interpreter
# symlinks are safe to delete. Run this after every `homey app dependencies
# add/install` (which recreates them), then validate / run again.
set -e
cd "$(dirname "$0")/.."
for arch in arm64 amd64; do
  rm -f "python_packages/$arch/.venv/bin/python" \
        "python_packages/$arch/.venv/bin/python3" \
        "python_packages/$arch/.venv/bin/python3.13" \
        "python_packages/$arch/.venv/lib64"
done
echo "Removed dangling venv symlinks. You can now run: homey app validate"
