#!/usr/bin/env bash
set -euo pipefail
project=$(cd -- "$(dirname -- "$0")" && pwd)
inputs=${WINDOWS_PACKAGE_INPUTS:-/workspace/.relay-windows}
output=$(realpath -m "$1")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
tar -xjf "$inputs/testdisk-source.tar.bz2" -C "$stage"
cd "$stage/testdisk-7.2"
patch -s -p1 < "$project/photorec-catalog.patch"
cp "$project"/catalog_io.{h,c} src/
headers=${JPEG_HEADERS:-/workspace/.testdisk-dev/root/usr/include}
config=${JPEG_CONFIG:-$headers/x86_64-linux-gnu/jconfig.h}
mkdir "$stage/deps"
cp "$headers"/{jpeglib.h,jerror.h,jmorecfg.h} "$config" /usr/include/{zlib.h,zconf.h} "$stage/deps/"
# The Windows libjpeg-62 ABI uses an 8-bit boolean.
cat >> "$stage/deps/jconfig.h" <<'JPEG_WINDOWS'
#if defined(_WIN32) && !defined(HAVE_BOOLEAN)
#ifndef __RPCNDR_H__
typedef unsigned char boolean;
#endif
#define HAVE_BOOLEAN 1
#endif
JPEG_WINDOWS
upstream="$inputs/upstream/testdisk-7.2"
ln -s "$upstream/libjpeg-62.dll" "$stage/deps/libjpeg.dll"
ln -s "$upstream/zlib1.dll" "$stage/deps/libz.dll"
export PATH="$inputs/mingw-shims:$inputs/mingw/usr/bin:$PATH"
./configure --host=x86_64-w64-mingw32 --disable-qt --without-ncurses --without-ntfs3g --without-ntfs \
    CPPFLAGS="-D_FILE_OFFSET_BITS=64 -I$stage/deps" LDFLAGS="-L$stage/deps" LIBS="-ljpeg -lz"
make -C src -j"${BUILD_JOBS:-8}" photorec.exe
x86_64-w64-mingw32-strip -o "$output" src/photorec.exe
