# Digger for Windows

Extract the ZIP and run **Digger.exe**. The x64 desktop app runs offline. Keep the extracted files together. The launcher is unsigned.

## Choose a scan

- **Recycle Bin** lists deleted files still in your Recycle Bin across local drives.
- **Folder in Recycle Bin** limits that search to a selected original folder and its subfolders.
- **NTFS folder** scans a Windows NTFS volume for deleted filesystem records and filters results to a selected folder. Launch the app as administrator for this mode and choose a destination on a different physical disk. The app reads the volume without modifying it. This mode requires validation on native Windows.
- **Filesystem image** lists deleted records in raw NTFS, FAT12/16/32, and exFAT volume or partitioned disk images. When the metadata survives, results retain the filesystem name and parent path. The app reads the image in place and extracts only the files you select. Keep the image unchanged between scan and recovery.
- **Deep scan image** uses PhotoRec to carve files from a raw disk image. It can find data after filesystem metadata is gone. PhotoRec assigns new filenames. This workflow needs temporary space for a read-only copy of the image and recovered files.
- **Inspect image** lists partitions with TestDisk. **Try a sample scan** lets you test PhotoRec without supplying an image.

## Select and recover

Use **By folder** or **By type** to organize matches. **Save report…** exports paths, sizes, dates, and recovery evidence as a CSV. Search names and original paths. Select an image or video to preview it. Zoom with the slider or mouse wheel. For live NTFS results and NTFS image files larger than 64 MiB, recover the file before previewing it. **Open** and **Play** use your Windows default app.

Select the files you want, click **Recover selected…**, and choose a destination. Digger checks available space, writes to a new folder, and publishes each file after the copy completes. It keeps surviving names and directory paths. FAT short names may have lost their first character. Recycle Bin entries stay available after copying.

For ordinary NTFS runs Digger checks cluster allocation before extraction. It rejects files whose checked clusters are currently allocated and rechecks a live volume before publishing the file. The checks are bounded, and results without a complete allocation check use a more limited status. A deleted NTFS record means the filesystem metadata is still present. Recovering the expected number of bytes does not prove that the original content survived an overwrite or SSD TRIM. Preview checks report whether an image or one video frame decoded.

The archive includes the CGSecurity TestDisk and PhotoRec engines, The Sleuth Kit, Python, Tk and FFmpeg. See the included licenses and source notices.
