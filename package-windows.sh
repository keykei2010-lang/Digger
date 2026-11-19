#!/usr/bin/env bash
set -euo pipefail
project=$(cd -- "$(dirname -- "$0")" && pwd)
inputs=${WINDOWS_PACKAGE_INPUTS:-/workspace/.relay-windows}
destination=${1:-/workspace/Digger-Windows-x64.zip}
destination=$(realpath -m "$destination")
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
root="$stage/Digger"
mkdir -p "$root"/{runtime/sleuthkit,engines/63,source,licenses}
cp "$project"/{desktop.py,server.py,carved_results.py,media_preview.py,recycle_bin.py,ntfs_recovery.py,windows_volumes.py,recovery_output.py} "$root"
cp "$project/WINDOWS.md" "$root/Read me.md"
cp "$project/launcher-win.c" "$project/tsk-windows-volume-length.patch" "$project/tsk-live-scan.patch" "$root/source"
compiler=${MINGW_CC:-"$inputs/mingw/usr/bin/x86_64-w64-mingw32-gcc-posix"}
"$compiler" -Os -s -mwindows "$project/launcher-win.c" -o "$root/Digger.exe"
unzip -q "$inputs/python.zip" -d "$root/runtime"
printf '%s\r\n' python313.zip . .. > "$root/runtime/python313._pth"
cp "$inputs/tk/DLLs/"{_tkinter.pyd,tcl86t.dll,tk86t.dll,zlib1.dll} "$root/runtime/"
cp -a "$inputs/tk/Lib/tkinter" "$root/runtime/"
mkdir "$root/runtime/tcl"
cp -a "$inputs/tk/tcl/"{tcl8.6,tk8.6,tcl8} "$root/runtime/tcl/"
unzip -q "$inputs"/pillow-*-cp313-cp313-win_amd64.whl -d "$root/runtime"
ffmpeg_wheel="$inputs/imageio_ffmpeg-0.6.0-py3-none-win_amd64.whl"
unzip -p "$ffmpeg_wheel" imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe > "$root/runtime/ffmpeg.exe"
unzip -p "$ffmpeg_wheel" imageio_ffmpeg-0.6.0.dist-info/LICENSE > "$root/licenses/ImageIO-FFmpeg.txt"
cp "$project/FFMPEG.txt" "$root/licenses/FFmpeg.txt"
upstream="$inputs/upstream/testdisk-7.2"
cp "$upstream"/{testdisk_win.exe,photorec_win.exe,cyg*.dll,COPYING.txt,VERSION.txt,readme.txt} "$root/engines/"
cp "$upstream/63/cygwin" "$root/engines/63/"
cp "$inputs/testdisk-source.tar.bz2" "$inputs/tsk-source.tar.gz" "$root/source/"
cp "$project/TSK.txt" "$root/licenses/SleuthKit.txt"
cp -a "$inputs/tsk-source/licenses" "$root/licenses/SleuthKit"
tsk="${TSK_BUILD_DIR:-$inputs/tsk-build-x64}"
for program in fls icat istat; do
  cp "$tsk/tools/fstools/$program.exe" "$root/runtime/sleuthkit/"
done
cp "$tsk/tools/vstools/mmls.exe" "$root/runtime/sleuthkit/"
strip_tool="$(dirname "$compiler")/x86_64-w64-mingw32-strip"
"$strip_tool" "$root"/runtime/sleuthkit/*.exe
for dll in libgcc_s_seh-1.dll libstdc++-6.dll libwinpthread-1.dll; do
  cp "$("$compiler" -print-file-name="$dll")" "$root/runtime/sleuthkit/"
done
cp "$inputs/mingw/usr/share/doc/gcc-mingw-w64-base/copyright" "$root/licenses/MinGW-GCC.txt"
cp "$inputs/mingw/usr/share/doc/mingw-w64-common/copyright" "$root/licenses/MinGW-w64.txt"
cp "$root/runtime/LICENSE.txt" "$root/licenses/Python.txt"
cp "$root"/runtime/pillow-*.dist-info/licenses/LICENSE "$root/licenses/Pillow.txt"
(cd "$stage" && zip -q -r "$stage/portable.zip" 'Digger')
chmod 644 "$stage/portable.zip"
mv "$stage/portable.zip" "$destination"
printf '%s\n' "$destination"
