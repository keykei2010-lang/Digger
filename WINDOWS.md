# Digger for Windows

Extract the ZIP and run **Digger.exe**. The x64 desktop app runs offline. Keep the extracted files together. The launcher is unsigned.

## Choose a scan

- **Deleted files** scans permanently deleted filesystem records on a mounted NTFS volume. Click a drive to search the whole volume, or enter the original folder path, such as `C:\Users\You\Pictures`, to limit the results. Use **Run as administrator** to authorize raw reads. Save recovered files to a different physical disk. Digger opens the source for reading. Indexed scans read the volume’s file table, even for a folder search. They show matching deleted records as they arrive, with a measured record count. A surviving emptied-bin payload may appear under its Windows $R name. Native Windows testing of live recovery is pending.
- **Recycle Bin** lists files Windows still holds in your Recycle Bin. Use **More scans → Recycle Bin by folder** to limit the search to the original folder.
- **Disk image** lists deleted records in raw NTFS, FAT12/16/32, and exFAT volume or partitioned disk images. When the metadata survives, results retain the filesystem name and parent path. The app reads the image in place and extracts only the files you select. Keep the image unchanged between scan and recovery.
- **Deep scan image** uses PhotoRec to carve files from a raw disk image. It can find data after filesystem metadata is gone. PhotoRec assigns new filenames. This workflow needs temporary space for a read-only copy of the image and recovered files.
- **Inspect image** lists partitions with TestDisk. **Try a sample scan** lets you test PhotoRec without supplying an image.

## Select and recover

Enter part of a filename before starting, or search while the scan runs. Checkboxes keep files selected across pages and filters. You can recover checked files during an indexed scan. Stopping keeps results already found.

Use **Folder** or **Type** in the Browse sidebar to filter results. Click a column to sort, or right-click a result to recover it or copy its original path. **Ctrl+L** focuses the source path, and **Ctrl+F** focuses results search. **Save report…** exports paths, sizes, dates, and recovery evidence as a CSV. Search names and original paths. Select an image or video to preview it. Zoom with the slider or mouse wheel. Digger previews live NTFS images up to 16 MiB in memory. For live videos or larger live images, recover the file first. The limit for previewing a file from a disk image is 64 MiB. **Open** and **Play** use your Windows default app.

Select the files you want, click **Recover selected…**, and choose a destination. Digger checks available space, writes to a new folder, and publishes each file after the copy completes. It keeps surviving names and directory paths. FAT short names may have lost their first character. Recycle Bin entries stay available after copying.

For ordinary NTFS runs Digger checks cluster allocation before extraction. It rejects files whose checked clusters are currently allocated and rechecks a live volume before publishing the file. The checks are bounded, and results without a complete allocation check use a more limited status. A deleted NTFS record means the filesystem metadata is still present. Recovering the expected number of bytes does not prove that the original content survived an overwrite or SSD TRIM. Preview checks report whether an image or one video frame decoded.

The archive includes the CGSecurity TestDisk and PhotoRec engines, The Sleuth Kit, Python, Tk and FFmpeg. See the included licenses and source notices.
