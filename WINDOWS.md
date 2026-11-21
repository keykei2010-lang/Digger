# Digger for Windows

Extract the ZIP and run **Digger.exe**. The x64 desktop app runs offline. Keep the extracted files together. The launcher is unsigned.

## Choose a scan

- **Deleted files** scans permanently deleted filesystem records on a mounted NTFS volume. Click a drive to search the whole volume, or enter the original folder path, such as `C:\Users\You\Pictures`, to limit the results. To authorize raw reads, right-click **Digger.exe** and choose **Run as administrator**. If you click Scan without that access, Digger shows a warning and keeps your current window open. Save recovered files to a different physical disk. Digger opens the source for reading. Indexed scans read the volume’s file table, even for a folder search. They show matching deleted records as they arrive, with a measured record count. A surviving emptied-bin payload may appear under its Windows $R name. Native Windows testing of live recovery is pending.
- **Formatted drive** reads raw sectors from a mounted drive letter using PhotoRec. Select **MP4 / MOV movies** to focus on those containers, or **All file types** for broader recovery. Choose a different physical disk with room for recovered files. The output folder persists after Digger closes. Carved files can lose their original names and may include current files. Overwrites, SSD TRIM, encryption and fragmentation can prevent recovery.
- **Recycle Bin** lists retained items and searches mounted NTFS volumes for emptied-bin records belonging to the current user. Surviving paired metadata can restore the original path. Use **Bin by folder** to limit the search to the original folder. The location screen shows whether this Digger process was launched as administrator. If a volume cannot be opened for raw reads, results show **Emptied-bin search unavailable** or **Emptied-bin search partial**, with the Windows error and any results already found.
- **Disk image** lists deleted records in raw NTFS, FAT12/16/32, and exFAT volume or partitioned disk images. When the metadata survives, results retain the filesystem name and parent path. The app reads the image in place and extracts only the files you select. Keep the image unchanged between scan and recovery.
- **Deep scan image** uses PhotoRec to carve files from a raw disk image. It can find data after filesystem metadata is gone. PhotoRec assigns new filenames. This workflow needs temporary space for a read-only copy of the image and recovered files.
- **Inspect image** lists partitions with TestDisk. **Test deleted-file recovery** opens a synthetic NTFS image in a temporary folder on a healthy drive you choose, recovers a known deleted file, and compares its SHA-256 digest with the original. The scan banner stays visible on both Locations and Results. Canceling changes to Scan canceled after the worker exits. Partial results remain available.

## Find older photos and videos from the Recycle Bin

Run Digger as administrator and choose **Recycle Bin** to include emptied records. Switch to **Grid** to browse thumbnails as results arrive. Choose **Filter → Photos and videos** to show only media. Under **Filter**, choose **Bin status → Emptied only** and the year under **Date deleted**. The date comes from the Windows bin record. Select **Unknown date** to include items whose deletion date was lost. Click a thumbnail to inspect it, then select the files to recover. The gallery and inspector can decode a frame from live deleted videos up to 16 MiB. Recover larger live videos to a separate disk for playback.

If bin or folder records are gone, select **Search a drive for missing media…** or **Deep scan this drive…**, then start the scan. PhotoRec searches the entire selected volume because files without filesystem records cannot be assigned to a folder or the Recycle Bin. Carved files may lose their original path, deletion date and connection to the bin.

## Select and recover

Enter part of a filename before starting, or search while the scan runs. Checkboxes keep files selected across pages and filters. You can recover checked files during an indexed scan. Stopping keeps results already found.

Use **Details** or **Grid** to switch views during scanning. Both views use the same results, filters, and checked files. Navigate result folders with the mouse Back and Forward buttons or Alt+Left and Alt+Right. Use **Path** or **Type** in the Browse sidebar to filter results. Click a column to sort, or right-click a result to recover it or copy its original path. **Ctrl+L** focuses the source path, and **Ctrl+F** focuses results search. **Save report…** exports paths, sizes, dates, and recovery evidence as a CSV. Search names and original paths. Select an image or video to preview it. Zoom with the slider or mouse wheel. Digger previews live NTFS images up to 16 MiB in memory. For live videos or larger live images, recover the file first. The limit for previewing a file from a disk image is 64 MiB. **Open** and **Play** use your Windows default app.

Select the files you want, click **Recover selected…**, and choose a destination. Digger checks available space, writes to a new folder, and publishes each file after the copy completes. It keeps surviving names and directory paths. FAT short names may have lost their first character. Recycle Bin entries stay available after copying.

For ordinary NTFS runs Digger checks cluster allocation before extraction. It rejects files whose checked clusters are currently allocated and rechecks a live volume before publishing the file. The checks are bounded, and results without a complete allocation check use a more limited status. A deleted NTFS record means the filesystem metadata is still present. Recovering the expected number of bytes does not prove that the original content survived an overwrite or SSD TRIM. Preview checks report whether an image or one video frame decoded.

The archive includes the CGSecurity TestDisk and PhotoRec engines, The Sleuth Kit, Python, Tk and FFmpeg. See the included licenses and source notices.

## Administrator access

Only scans that read a drive directly need it: **Deleted files**, **Formatted drive**, and the emptied-bin part of **Recycle Bin**. Disk images and items still in the Recycle Bin work without it. When a scan needs administrator access, Digger tells you in a pop-up, and Recycle Bin scans still list the retained items. To elevate, right-click Digger.exe and choose Run as administrator.
