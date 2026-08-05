# CameraCopy

CameraCopy is a cross-platform desktop application for copying photos and videos from
camera cards (and other mounted storage volumes.) to an organized destination folder. It supports
verified copies, dual-volume clone workflows, metadata-based folders, sidecars, optional source
cleanup, and guarded post-copy formatting.

## Features

- Copy from one or two mounted volumes.
- Dual-volume clone verification with mismatch handling.
- Recursive scanning with configurable include and exclude patterns.
- Date-based destination folders with configurable naming.
- Ratings from Adobe XMP and RapidRaw `.rrdata` sidecars.
- Optional embedded ratings and timestamps through ExifTool.
- Optional copying of matching XMP and RapidRaw sidecars.
- Existing-file handling: ask, skip, overwrite, or keep both.
- SHA-256 verification and temporary-file writes.
- Optional source cleanup after successful preservation.
- Guarded formatting after a clean copy operation.
- Per-operation logs, progress, transfer speed, ETA, and cancellation.
- System, light, and dark themes with customizable copy output colors.

## Windows requirements

The Windows installer and portable package are self-contained; Python is not required.

- A 64-bit Windows system.
- CameraCopy runs without Administrator privileges. If Windows denies a format operation,
  CameraCopy requests UAC elevation for that operation only.
- [ExifTool](https://exiftool.org/install.html) is optional and enables embedded ratings
  and capture timestamps.

Download either the installer or portable archive from the repository's Releases page:

```text
CameraCopy-Setup-<version>.exe
CameraCopy-<version>-windows-x86_64-portable.zip
```

The portable archive must be extracted before use. Keep `CameraCopy.exe` together with the
files in its distribution folder.

## Linux requirements

Native x86-64 packages are built for Debian/Ubuntu, Fedora/RPM-based systems, and Arch
Linux. Package managers install the required runtime libraries automatically.
CameraCopy uses UDisks2 for unmounting, formatting, and remounting removable media.
Formatting may require authorization through the system's Polkit configuration.
[ExifTool](https://exiftool.org/) is optional and enables embedded ratings and
capture timestamps.

Install a downloaded package with the appropriate command:

```bash
# Debian / Ubuntu
sudo apt install ./cameracopy_<version>_amd64.deb

# Fedora / RPM-based distributions
sudo dnf install ./cameracopy-<version>-1.x86_64.rpm

# Arch Linux
sudo pacman -U ./cameracopy-<version>-1-x86_64.pkg.tar.zst
```

Start CameraCopy from the desktop menu or run:

```bash
cameracopy
```
### Manual Linux installation

The supplied Linux packages are recommended because they install CameraCopy's dependencies
automatically. To install the source tree manually as a normal system application, first make sure Python
3.11 or newer and the dependencies listed in `requirements.txt` are installed.

From the extracted CameraCopy source directory, run:

```bash
sudo mkdir -p /usr/lib/cameracopy
sudo cp -R cameracopy2 /usr/lib/cameracopy/

sudo install -m 755 packaging/linux/cameracopy /usr/bin/cameracopy
sudo install -m 644 packaging/linux/cameracopy.desktop \
    /usr/share/applications/cameracopy.desktop

sudo cp -R packaging/linux/icons/hicolor/* /usr/share/icons/hicolor/
```

CameraCopy can then be opened from the desktop application menu or started with:

```bash
cameracopy
```

## Usage

1. Open **Settings**.
2. Set the source subfolder, commonly `DCIM`, or leave it empty to scan the selected volume root.
3. Choose the destination folder and file patterns.
4. Configure folder naming, metadata, verification, sidecar, and collision options.
5. Select the first source volume in the main window.
6. Optionally select a second volume and enable clone mode.
7. Start the copy and review the final summary before removing or formatting media.


## Building

### Development environment

CameraCopy requires [Python 3.11 or newer](https://www.python.org/downloads/) and
[Qt for Python / PySide6](https://doc.qt.io/qtforpython-6/).

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
python -m cameracopy2
```

On Windows, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

## Safety and disclaimer

CameraCopy handles valuable and sometimes irreplaceable files. Verify copied files
independently before deleting or formatting source media, and test destructive workflows
with disposable media first.

The software is provided without warranty. The authors and contributors are not responsible
for lost, damaged, corrupted, or overwritten data, hardware problems, or other losses
resulting from its use. **USE AT YOUR OWN RISK!**
