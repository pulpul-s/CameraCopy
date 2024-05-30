﻿# CameraCopy

A straightforward Powershell GUI tool for copying files from memory card or camera to your computer.<br><br>
Scans the system for available volumes, filters the volume list by user keywords, checks file timestamps on source, creates a timestamped directory, copies files, checks sha256 hash, removes copied files and formats source volume. Does not overwrite already existing files.

<img src="./screenshots/1.png" alt="Main window"><br>
<img src="./screenshots/2.png" width="500px" alt="Copy window and format confirmation">

## Configuration

Edit cameracopy.json by hand or click the cogwheel icon.

```json
{
    "source": "DCIM\\",                 <- Source folder of images, does not have to be set. Will only use volume letter if not set.
    "destination": "D:\\Pictures",      <- Destination folder for files, must have full path.
    "includedfiles": [                  <- List of files to include in copy, cameras might have additional files. Set to "*" if you want to copy everything.
        "*.arw",
        "*.mp4"
    ],
    "excludedfiles": [                  <- Files to exclude from copy progress. Can be left empty.
        "*thumb*",
        "ABC06591.ARW"
    ],
    "includeddevices": [                <- Volume dropdown filter keywords. If left empty, all found volumes will be listed.
        "Sony",
        "DataTraveler"
    ],
    "folderprefix": "prefix_",          <- Destination folder prefix.
    "datetimestring": "yyyy-MM-dd",     <- Powershell datetime string used in destination folder. yyyy-MM-dd meaning 2024-01-01.
    "folderpostfix": "_postfix",        <- Destination folder postfix.
    "defaultdevice": "0",               <- If you know your device is always e.g. second on the list set to 1.
    "minrating":  "0",                  <- Sets a minimum metadata Rating for files. Does not copy if no rating is found. 0 is off.
    "autoformat": "exFAT",              <- Select format filesystem on start. Can be empty or any of FAT32, exFAT, NTFS
    "autoremove": false,                <- Select remove files after copying on start
    "formatprompt": true,               <- Prompt before formatting, set to false if you want to format without confirmation (dangerous).
    "checkhash": true,                  <- When true, do a SHA256 hash check fo each file after copy (slows down process).
    "overwrite": false,                 <- Overwrite existing files
    "fixsonytimestamps": true           <- Sony cameras sometimes have different CreationDate for the video files than the actual shooting time. Get the actual time from XML
}
```

### Notes
* Copy source will always be '{selectedDrive}:\\{source}\\' e.g. 'F:\\DCIM\\'
* Copy destination will always be '{destination}\\{folderprefix}{datetimestring}{folderpostfix}\\' e.g. 'D:\Pictures\2024-01-01\\'
* Do not add a leading drive to source e.g. 'D:\\DCIM\\'
* folderprefix, folderpostfix and datetimestring can be left empty. Files are then copied to destination.
* autoformat and autoremove only affect which option is default in the gui.
* Does not support MTP devices. Devices must have an assigned volume letter.
* SHA256 hash check should not be necessary, since USB file transfer is error correcting. However if you suspect the integrity of the file transfer, you can enable the option. If a hash check fails, the source files that fail the hash check will not be removed, even when auto remove is enabled.
* If files fail the hash check, you will be prompted to delete the files.
* minrating 0< first checks the file metadata for Rating, if not found it looks it from $filename.xmp file. Files without rating are rated 0.
* Icons might be a bit off if you covert the script to exe with ps2exe.
