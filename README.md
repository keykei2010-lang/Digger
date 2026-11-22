# Digger

Digger is a portable, offline Windows x64 data recovery app. Scan a mounted NTFS drive or original folder for deleted records, search retained and emptied Recycle Bin entries, deep scan a formatted drive for surviving file contents, or scan a raw disk image. Browse by original folder or file type, preview media, and recover selected files to a destination you choose.

See [Windows usage](WINDOWS.md) for supported sources, recovery checks, and current limits. Live NTFS folder recovery requires administrator access, a separate physical destination disk, and testing on native Windows.

Deep scans index the source in place. Preview results and choose the files to recover before choosing a destination.

## Source and packaging

- `desktop.py` runs the Windows interface. `launcher-win.c` builds its double-click launcher.
- `package-windows.sh` builds the portable ZIP with a MinGW toolchain and the prepared dependency directory specified by `WINDOWS_PACKAGE_INPUTS`. The inputs it consumes are listed in the script. [Sleuth Kit build notes](TSK.txt) and [FFmpeg notices](FFMPEG.txt) describe those engines.
- `catalog/build.sh` builds the Windows PhotoRec catalog engine from the upstream 7.2 source archive and the included patch. Its format checks read source extents directly. `sector_catalog.py` uses the same extents for preview and recovery.
- The repository also contains the local browser interface used during earlier development. The Windows desktop launcher runs independently of it.

The portable build includes separate third-party engines with their upstream license and source notices.
