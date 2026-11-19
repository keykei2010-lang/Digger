#!/usr/bin/env bash
set -euo pipefail
project=$(cd -- "$(dirname -- "$0")" && pwd)
checkout=${TESTDISK_SOURCE_DIR:-/workspace/testdisk}
engines=${RECOVERY_BIN_DIR:-/workspace/.testdisk-dev/build/src}
destination=${1:-/workspace/Digger-Linux-x86_64.zip}
destination=$(realpath -m "$destination")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/Digger"
mkdir -p "$root"/{engines,licenses,source,runtime/bin,runtime/lib,runtime/share/terminfo/x}
cp "$project"/{app.css,app.js,index.html} "$root"
cp "$project"/{launch.sh,server.py,README.md} "$root"
cp /usr/bin/python3 "$root/runtime/bin/python3"
cp -a /usr/lib/python3.13 "$root/runtime/lib/"
rm -rf "$root/runtime/lib/python3.13/site-packages"
find "$root/runtime/lib/python3.13" -type d -name __pycache__ -prune -exec rm -rf '{}' +
cp /usr/share/terminfo/x/xterm "$root/runtime/share/terminfo/x/"
for engine in testdisk photorec; do
  cp "$engines/$engine" "$root/engines/$engine.bin"
  cat > "$root/engines/$engine" <<'WRAPPER'
#!/bin/sh
set -eu
path=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
exec /lib64/ld-linux-x86-64.so.2 --inhibit-rpath '' --library-path "$path/../runtime/lib" "$0.bin" "$@"
WRAPPER
  chmod +x "$root/engines/$engine"
done
extensions="$root/runtime/lib/python3.13/lib-dynload"
find "$extensions" -maxdepth 1 -name '*.so' \
  ! -name '_bz2.*' ! -name '_hashlib.*' ! -name '_lzma.*' ! -name '_ssl.*' -delete
deps=$(ldd "$engines"/{testdisk,photorec} /usr/bin/python3 "$extensions"/*.so)
if [[ "$deps" == *"not found"* ]]; then
  printf '%s\n' "$deps" >&2
  exit 1
fi
while read -r soname path; do
  case "$soname" in
    libc.so.*|libm.so.*|libdl.so.*|libpthread.so.*|librt.so.*|libresolv.so.*|ld-linux*) continue ;;
  esac
  cp -L "$path" "$root/runtime/lib/$soname"
  if [[ "$path" = /workspace/.testdisk-dev/root/* ]]; then
    stem=${soname%%.so*}
    [[ "$stem" != libcom_err ]] || stem=libcom-err
    owner=$(find /workspace/.testdisk-dev/root/usr/share/doc -maxdepth 2 -name copyright -path "*/$stem*/copyright" -print -quit)
  else
    pkg=$(dpkg-query -S "$(realpath "$path")" 2>/dev/null | head -n 1 | cut -d: -f1 || true)
    owner="/usr/share/doc/$pkg/copyright"
  fi
  if [[ -r "$owner" ]]; then cp -L "$owner" "$root/licenses/$soname.txt"; fi
done < <(printf '%s\n' "$deps" | awk '$2 == "=>" && $3 ~ /^\// { print $1, $3 }' | sort -u)
cp /usr/share/doc/python3.13/copyright "$root/licenses/Python.txt"
cp "$checkout/COPYING" "$root/licenses/TestDisk-PhotoRec-GPL-2.txt"
git -C "$checkout" archive --format=tar.gz --prefix=testdisk/ HEAD > "$root/source/testdisk-source.tar.gz"
git -C "$checkout" rev-parse HEAD > "$root/source/testdisk-commit.txt"
(cd "$stage" && zip -q -r "$stage/portable.zip" Digger)
chmod 644 "$stage/portable.zip"
mv "$stage/portable.zip" "$destination"
printf '%s\n' "$destination"
