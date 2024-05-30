# https://github.com/pulpul-s/CameraCopy
$version = "1.4.1"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

[System.Windows.Forms.Application]::EnableVisualStyles()

function Scan {
    # Get the list of volumes
    $volumes = Get-Volume | Where-Object { $null -ne $_.DriveLetter }

    # Initialize an array to hold the output
    $output = @()

    # Loop through each volume and get the required information
    foreach ($volume in $volumes) {
        $driveLetter = $volume.DriveLetter
        $sizeGB = [math]::Round($volume.Size / 1GB, 3)
    
        # Get the physical disk related to the volume
        $partition = Get-Partition -DriveLetter $driveLetter
        $diskNumber = $partition.DiskNumber
        $physicalDisk = Get-PhysicalDisk | Where-Object { $_.DeviceId -eq $diskNumber }
        $hardwareName = $physicalDisk.FriendlyName

        # Construct the output string
        $outputString = "${driveLetter}:  $hardwareName - $sizeGB GB"
        $output += $outputString
    }
    return $output

}


function SplashScan {

    # Create the splash screen form
    $splashForm = New-Object System.Windows.Forms.Form
    $splashForm.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon("assets\cameracopy.ico")
    $splashForm.Text = "Scanning drives..."
    $splashForm.Size = New-Object System.Drawing.Size(300, 225)
    $splashForm.StartPosition = "CenterScreen"
    $splashForm.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
    $splashForm.BackgroundImage = [system.drawing.image]::FromFile("assets\splashback.png")

    # Create a label for the splash screen
    $splashLabel = New-Object System.Windows.Forms.Label
    $splashLabel.BackColor = [System.Drawing.Color]::FromArgb(0, 255, 255, 255)
    $splashLabel.Text = "Scanning drives..."
    $splashLabel.AutoSize = $true
    $splashLabel.Font = [System.Drawing.Font]::new("Microsoft Calibri", 11)
    $splashLabel.Location = New-Object System.Drawing.Point(0, 206)
    $splashForm.Controls.Add($splashLabel)

    # Show the splash screen
    $splashForm.Show()
    $splashForm.Refresh()
    $drives = Scan

    $drives = $drives | Sort-Object
    $splashLabel.Text = "Reading config..."
    $config = Get-Content -Path "cameracopy.json" -Raw | ConvertFrom-Json
    return $drives, $splashForm, $config
}


function CopyFiles {
    param(
        [string]$sourcePath,
        [string]$destinationPath,
        [bool]$autoremove,
        [string]$format,
        [string]$drive,
        [string]$driveDescription
    )

    $form = New-Object System.Windows.Forms.Form
    $form.Text = "Copy progress"
    $form.Size = New-Object System.Drawing.Size(950, 450)
    $form.StartPosition = "CenterScreen"
    $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon("assets\cameracopy.ico")
    $form.BackColor = [System.Drawing.Color]::White

    $textBox = New-Object System.Windows.Forms.TextBox
    $textBox.Multiline = $true
    $textBox.ScrollBars = "Vertical"
    $textBox.Dock = "Fill"
    $textBox.BackColor = [System.Drawing.Color]::White
    $textBox.ForeColor = [System.Drawing.Color]::Black
    $textBox.Font = New-Object System.Drawing.Font("Consolas", 10)
    $textBox.ReadOnly = $true

    $form.Controls.Add($textBox)

    $syncHash = [hashtable]::Synchronized(@{ 
            LogMessages = [System.Collections.ArrayList]::Synchronized((New-Object System.Collections.ArrayList))
            Cancel      = $false
        })

    # Create a runspace pool
    $runspacePool = [runspacefactory]::CreateRunspacePool(1, [Environment]::ProcessorCount)
    $runspacePool.Open()

    # Define the script block for the runspace
    $scriptBlock = {
        param($syncHash, $sourcePath, $destinationPath, $config, $autoremove, $format, $drive, $driveDescription)

        function FormatDrive {
            try {
                $syncHash.LogMessages.Add("Formatting volume ${drive}: to $format`r`n")
                Format-Volume -DriveLetter $drive -FileSystem $format -Confirm:$false
                $syncHash.LogMessages.Add("Volume ${drive}: formatted successfully as $format.")
            }
            catch {
                $syncHash.LogMessages.Add("Failed to format the volume: ${drive}:")
            }
        }

        try {
            $files = Get-ChildItem -Path $sourcePath -File -Recurse | Where-Object {
                $file = $_
                # Check if the file matches any of the inclusion patterns
                $matchesIncluded = $config.includedfiles | ForEach-Object { $file.Name -like $_ }
                # Check if the file matches any of the exclusion patterns
                $matchesExcluded = $config.excludedfiles | ForEach-Object { $file.Name -like $_ }
                # Include the file only if it matches an inclusion pattern and does not match an exclusion pattern
                ($matchesIncluded -contains $true) -and ($matchesExcluded -notcontains $true)
            }
            $copyCompleted = $false
            $fileCount = $files.Count
            $syncHash.LogMessages.Add("Found $fileCount files in $sourcePath`r`n")

            if ($autoremove) {
                $syncHash.LogMessages.Add("Files are marked for removal after copying!`r`n")
            }

            if ($fileCount -eq 0) {
                $syncHash.LogMessages.Add("No eligible files found in the source directory.`r`n")
                return
            }
            
            $syncHash.LogMessages.Add("Starting copy...`r`n")
            Start-Sleep -Milliseconds 200
            $copyStartTime = Get-Date
            $totalFiles = $fileCount
            $filesCopied = 0
            $destinationRoot = $destinationPath
            $hashFailFiles = @()

            $skipFileCount = 0
            $copyFileCount = 0

            if ($config.minrating -ge 1) {
                $shell = New-Object -ComObject Shell.Application
                $ratingFolder = $shell.NameSpace($sourcePath)
            }

            foreach ($file in $files) {
                if ($syncHash.Cancel) {
                    $syncHash.LogMessages.Add("Operation canceled.`r`n")
                    break
                }

                # Extract the creation date of the file
                if ($config.datetimestring) { 
                    $creationDate = $file.CreationTime.ToString($config.datetimestring) 
                }

                # Construct the full path
                $childPath = $config.folderprefix + $creationDate + $config.folderpostfix
                    
                # Construct the destination folder path
                $destinationPath = Join-Path -Path $destinationRoot -ChildPath $childPath

                # Copy the file to the destination folder
                $destinationFile = Join-Path -Path $destinationPath -ChildPath $file.Name

                if ($config.minrating -ge 1 -and (-not (Test-Path -Path $destinationFile) -or $config.overwrite)) {
                    $fileItem = $ratingfolder.ParseName((Split-Path $file -Leaf))
                    $rating = $ratingfolder.GetDetailsOf($fileItem, 19) -replace '[^\d]', ''
                }

                $xmpfile = [System.IO.Path]::ChangeExtension($file.FullName, "xmp")
                if (-not $rating -and $config.minrating -ge 1 -and (Test-Path -Path $xmpfile)) {
                    $xmlContent = Get-Content -Path $xmpfile -Raw
                    $xmlContent -match 'xmp:Rating="(\d+)"'
                    $rating = $matches[1]
                }

                if (-not $rating -and $config.minrating -ge 1) {
                    $rating = 0
                }
                
                if (($rating -ge $config.minrating -or $config.minrating -eq 0) -and (-not (Test-Path -Path $destinationFile) -or $config.overwrite)) {
                    try {

                        # Create the folder if it does not exists
                        if (-not (Test-Path -Path $destinationPath)) {
                            New-Item -ItemType Directory -Path $destinationPath | Out-Null
                        }

                        # Copy file
                        Copy-Item -Path $file.FullName -Destination $destinationFile -Force
                            
                        # Preserve the timestamps
                        $destinationFileInfo = Get-Item -Path $destinationFile
                        $destinationFileInfo.CreationTime = $file.CreationTime
                        $destinationFileInfo.LastWriteTime = $file.LastWriteTime
                            
                        $filesCopied++
                        $progressPercentage = [Math]::Round(($filesCopied / $totalFiles) * 100, 1)
                        $progressPercentage = "{0:n1}" -f $progressPercentage

                        # Check copy integrity SHA256
                        $hashCheck = $true
                        if ($config.checkhash) {
                            $sourceFileHash = (Get-FileHash -Path $file.FullName -Algorithm SHA256).Hash
                            $destinationFileHash = (Get-FileHash -Path $destinationFile -Algorithm SHA256).Hash
                            $hashCheck = ($sourceFileHash -eq $destinationFileHash)
                            $syncHash.LogMessages.Add("Copied $($file.FullName) to $destinationFile ($progressPercentage % complete, SHA256 match $hashCheck)`r`n")
                        } 
                        else {
                            $syncHash.LogMessages.Add("Copied $($file.FullName) to $destinationFile ($progressPercentage % complete)`r`n")
                        }

                        if (-not $hashCheck) {
                            $hashFailFiles += $destinationFile
                        }

                        if ($autoremove -and (Test-Path $destinationFile) -and $hashCheck) {
                            Remove-Item -Path $file.FullName
                            $syncHash.LogMessages.Add("Removed file $($file.FullName)`r`n")
                        }
                        $copyFileCount++
                    }
                    catch {
                        $syncHash.LogMessages.Add("Error while processing $($file): $($_.Exception.Message)`r`n")
                    }
                }
                elseif ($config.minrating -ne 0 -and $rating -lt $config.minrating -and (Test-Path -Path $destinationFile) -and $config.overwrite) {
                    $skipFilecount++
                    $filesCopied++
                    $progressPercentage = [Math]::Round(($filesCopied / $totalFiles) * 100, 1)
                    $progressPercentage = "{0:n1}" -f $progressPercentage
                    $syncHash.LogMessages.Add("$($file.FullName) not overwritten, too low($rating) rating. ($progressPercentage % complete)`r`n")
                }
                elseif ($config.minrating -ne 0 -and $rating -lt $config.minrating -and -not (Test-Path -Path $destinationFile)) {
                    $skipFilecount++
                    $filesCopied++
                    $progressPercentage = [Math]::Round(($filesCopied / $totalFiles) * 100, 1)
                    $progressPercentage = "{0:n1}" -f $progressPercentage
                    $syncHash.LogMessages.Add("$($file.FullName) not copied, too low($rating) rating. ($progressPercentage % complete)`r`n")
                }
                else {
                    $skipFilecount++
                    $filesCopied++
                    $progressPercentage = [Math]::Round(($filesCopied / $totalFiles) * 100, 1)
                    $progressPercentage = "{0:n1}" -f $progressPercentage
                    $syncHash.LogMessages.Add("$($destinationFile) exists, not replaced. ($progressPercentage % complete)`r`n")
                }
            }

            $copyCompleted = $true
            if (-not $syncHash.Cancel) {
                $copyEndTime = Get-Date
                $elapsedTime = $copyEndTime - $copyStartTime
                $elapsedTimeInSeconds = [math]::Round($elapsedTime.TotalSeconds, 0)
                Write-Output "Elapsed time: $elapsedTimeInSeconds seconds"
                $syncHash.LogMessages.Add("$copyFileCount files copied, $skipFilecount skipped in $($elapsedTimeInSeconds) seconds`r`n")
            }

            $verifyHashFailDelete = $null
            if ($hashFailFiles -and $config.checkhash) {
                $syncHash.LogMessages.Add("Following files($($hashFailFiles.Count)) failed the hash check and their source has not been removed:`r`n")
                foreach ($file in $hashFailFiles) {
                    $syncHash.LogMessages.Add("$($file)`r`n")
                }
                $verifyHashFailDelete = [System.Windows.Forms.MessageBox]::Show("Do you want to delete the files that failed the hash check?`r`n", "Delete confirmation", [System.Windows.Forms.MessageBoxButtons]::YesNo)
            }

            if ($verifyHashFailDelete -eq "Yes") {
                foreach ($file in $hashFailFiles) {
                    try {
                        Remove-Item -Path $file
                        $syncHash.LogMessages.Add("Deleted $file`r`n")
                    }
                    catch {
                        $syncHash.LogMessages.Add("Failed to delete $file`r`n")
                    }
                }
            }

            $verifyFormat = $null
            if ($format -and $copyCompleted -and $config.formatprompt) {
                $verifyFormat = [System.Windows.Forms.MessageBox]::Show("This will format $driveDescription to $format`r`nDo you want to continue?", "Format confirmation", [System.Windows.Forms.MessageBoxButtons]::YesNo)
                $syncHash.LogMessages.Add("$verifyFormat`r`n")
            }
            elseif ($format -and $copyCompleted -and -not $config.formatprompt) {
                FormatDrive
            }

            if ($format -and $copyCompleted -and $verifyFormat -eq "Yes") {
                FormatDrive
            }
            

        }
        catch {
            $syncHash.LogMessages.Add("Error: $_`r`n")
        }
    }
    

    # Create a PowerShell instance and add the script block to it
    $powerShell = [powershell]::Create().AddScript($scriptBlock).AddArgument($syncHash).AddArgument($sourcePath).AddArgument($destinationPath).AddArgument($config).AddArgument($autoremove).AddArgument($format).AddArgument($drive).AddArgument($driveDescription)

    # Associate the runspace pool with the PowerShell instance
    $powerShell.RunspacePool = $runspacePool

    # Start the PowerShell instance asynchronously
    $asyncResult = $powerShell.BeginInvoke()

    # Timer to update the TextBox periodically
    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 50 # Update every 50 ms
    $timer.Add_Tick({
            if ($syncHash.LogMessages.Count -gt 0) {
                $form.Invoke([Action] {
                        while ($syncHash.LogMessages.Count -gt 0) {
                            $message = $syncHash.LogMessages[0]
                            $syncHash.LogMessages.RemoveAt(0)
                            $textBox.AppendText($message)
                            $textBox.ScrollToCaret()
                        }
                    })
            }
        })
    $timer.Start()

    # Handle form close event to cancel the operation
    $form.add_FormClosing({
            $syncHash.Cancel = $true
            $timer.Stop()
            $powerShell.Stop()
            $runspacePool.Close()
            $runspacePool.Dispose()
            $form.Dispose()
        })

    # Show the form
    $form.ShowDialog()

    # Clean up resources
    $timer.Stop()
    $powerShell.EndInvoke($asyncResult)
    $powerShell.Dispose()
}


function SettingsForm {
    # Read JSON file
    $jsonContent = Get-Content -Path "cameracopy.json" | Out-String
    $jsonObject = ConvertFrom-Json -InputObject $jsonContent

    # Create form
    $settingsForm = New-Object System.Windows.Forms.Form
    $settingsForm.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon("assets\cameracopy.ico")
    $settingsForm.Text = "Configuration"
    $settingsForm.Size = New-Object System.Drawing.Size(350, 550)
    $settingsForm.StartPosition = "CenterScreen"
    $settingsForm.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedSingle

    # Create labels and textboxes dynamically from JSON fields
    $yPos = 20
    foreach ($propertyName in $jsonObject.PSObject.Properties.Name) {
        $label = New-Object System.Windows.Forms.Label
        $label.Text = $propertyName
        $label.AutoSize = $true
        $label.Location = New-Object System.Drawing.Point(20, $yPos)
    
        if (($jsonObject.$propertyName -eq $true -or $jsonObject.$propertyName -eq $false -or $propertyName -eq "autoformat") -and $propertyName -ne "defaultdevice") {
            $comboBox = New-Object System.Windows.Forms.ComboBox
            $comboBox.Location = New-Object System.Drawing.Point(150, $yPos)
            $comboBox.Size = New-Object System.Drawing.Size(160, 20)
            $comboBox.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList    
            $comboBox.Name = $propertyName

            if ($propertyName -eq "autoformat") {
                $comboBox.Items.Add("") | Out-Null
                $comboBox.Items.Add("FAT32") | Out-Null
                $comboBox.Items.Add("exFAT") | Out-Null
                $comboBox.Items.Add("NTFS") | Out-Null
            }
            else {
                $comboBox.Items.Add("True") | Out-Null
                $comboBox.Items.Add("False") | Out-Null
            }

            $selectedItem = if ($jsonObject.$propertyName -eq $true) { 
                "True" 
            } 
            elseif ($jsonObject.$propertyName -eq $false) { 
                "False" 
            } 
            else { 
                $jsonObject.$propertyName.ToString() 
            }
            $comboBox.SelectedIndex = $comboBox.Items.IndexOf($selectedItem)

            $yPos += 30
            $settingsForm.Controls.Add($comboBox)
        }
        else {
            $textBox = New-Object System.Windows.Forms.TextBox
            $textBox.Location = New-Object System.Drawing.Point(150, $yPos)
            $textBox.Size = New-Object System.Drawing.Size(160, 20)
            $textBox.Name = $propertyName  # Assigning property name as the control name
    
            if ($jsonObject.$propertyName -is [System.Collections.IList]) {
                $textBox.Text = $jsonObject.$propertyName -join ";"
            }
            else {
                $textBox.Text = $jsonObject.$propertyName
            }
    
            $yPos += 30
            $settingsForm.Controls.Add($textBox)
        }
        
        $settingsForm.Controls.Add($label)

    }
    # Add Save button
    $button = New-Object System.Windows.Forms.Button
    $button.Text = "Save"
    $button.Location = New-Object System.Drawing.Point(137, ($yPos + 5))
    $button.Size = New-Object System.Drawing.Size(75, 23)
    $button.Add_Click({
            foreach ($control in $settingsForm.Controls) {
                $propertyName = $control.Name
                if ($control -is [System.Windows.Forms.ComboBox] -and $propertyName -ne "autoformat") {
                    $jsonObject.$propertyName = [bool]::Parse($control.SelectedItem)
                }
    
                if ($control -is [System.Windows.Forms.ComboBox] -and $propertyName -eq "autoformat") {
                    $jsonObject.$propertyName = $control.SelectedItem
                }
    
                if ($control -is [System.Windows.Forms.TextBox] -and $jsonObject.$propertyName -is [System.Collections.IList]) {
                    $jsonObject.$propertyName = @($control.Text -split ';')

                }
    
                if ($control -is [System.Windows.Forms.TextBox] -and $jsonObject.$propertyName -isnot [System.Collections.IList]) {
                    $jsonObject.$propertyName = $control.Text
                }
            }
    
            try {
                $jsonObject | ConvertTo-Json | Set-Content -Path "cameracopy.json"
                [System.Windows.Forms.MessageBox]::Show("Configuration saved successfully.`r`nCameraCopy will restart if you made changes.")
            }
            catch {
                [System.Windows.Forms.MessageBox]::Show("Could not save file cameracopy.json`r`nMake sure you have write privileges or run as Admin.")
            }
        })
    $settingsForm.Controls.Add($button)

    # Display form
    $settingsForm.ShowDialog()

}


function Main {
    param(
        $drives,
        $config
    )

    # Create the form
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "CameraCopy " + $version
    $form.Size = New-Object System.Drawing.Size(305, 200)
    $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedSingle
    $form.StartPosition = "CenterScreen"
    $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon("assets\cameracopy.ico")

    # Create a ComboBox for drive selection
    $label = New-Object System.Windows.Forms.Label
    $label.Text = "Source Drive:"
    $label.Size = New-Object System.Drawing.Size(175, 20)
    $label.Location = New-Object System.Drawing.Point(10, 20)
    $form.Controls.Add($label)

    $filterWords = @($config.includeddevices)

    $comboBox = New-Object System.Windows.Forms.ComboBox
    $comboBox.Size = New-Object System.Drawing.Size(262, 20)
    $comboBox.DropDownStyle = [System.Windows.Forms.ComboBoxStyle]::DropDownList
    $comboBox.Location = New-Object System.Drawing.Point(10, 40)

    foreach ($drive in $drives) {
        if ($filterWords.Count -eq 0 -or ($filterWords | Where-Object { $drive -match $_ }).Count -gt 0) {
            $comboBox.Items.Add($drive)
        }
    }

    if ($comboBox.Items.Count -eq 0) {
        $comboBox.Items.Add("No drives found.")
    }
    $comboBox.SelectedIndex = $config.defaultdevice
    $form.Controls.Add($comboBox)


    $saveClose = $false
    # refresh icon
    $refreshPictureBox = New-Object System.Windows.Forms.PictureBox
    $refreshPictureBox.Size = New-Object System.Drawing.Size(20, 20)
    $refreshPictureBox.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::StretchImage
    $refreshPictureBox.Location = New-Object System.Drawing.Point(275, 40)
    $refreshPictureBox.Image = [System.Drawing.Image]::FromFile("assets/refresh.png")
    $refreshPictureBox.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::StretchImage
        
    $refreshPictureBox.Add_Click({
            $saveClose = $true
            $form.Close()
        })
        
    $form.Controls.Add($refreshPictureBox)


    # Format checkboxes
    $label = New-Object System.Windows.Forms.Label
    $label.Text = "Format:"
    $label.Location = New-Object System.Drawing.Point(10, 70)
    $label.AutoSize = $true
    $form.Controls.Add($label)

    $checkboxFAT32 = New-Object System.Windows.Forms.CheckBox
    $checkboxFAT32.Text = "FAT32"
    $checkboxFAT32.Location = New-Object System.Drawing.Point(70, 70)
    $checkboxFAT32.AutoSize = $true
    $form.Controls.Add($checkboxFAT32)
    if ($config.autoformat -eq "FAT32") { $checkboxFAT32.Checked = $true }

    $checkboxExFAT = New-Object System.Windows.Forms.CheckBox
    $checkboxExFAT.Text = "exFAT"
    $checkboxExFAT.Location = New-Object System.Drawing.Point(130, 70)
    $checkboxExFAT.AutoSize = $true
    $form.Controls.Add($checkboxExFAT)
    if ($config.autoformat -eq "exFAT") { $checkboxExFAT.Checked = $true }

    $checkboxNTFS = New-Object System.Windows.Forms.CheckBox
    $checkboxNTFS.Text = "NTFS"
    $checkboxNTFS.Location = New-Object System.Drawing.Point(190, 70)
    $checkboxNTFS.AutoSize = $true
    $form.Controls.Add($checkboxNTFS)
    if ($config.autoformat -eq "NTFS") { $checkboxNTFS.Checked = $true }

    # Function to handle checkbox clicks
    $checkboxClickHandler = {
        param ($sender)

        # Toggle the checkbox state
        if ($sender.Checked) {
            # Uncheck all checkboxes
            $checkboxFAT32.Checked = $false
            $checkboxExFAT.Checked = $false
            $checkboxNTFS.Checked = $false
            # Check the clicked checkbox
            $sender.Checked = $true
        }
    }
    $checkboxFAT32.Add_Click($checkboxClickHandler)
    $checkboxExFAT.Add_Click($checkboxClickHandler)
    $checkboxNTFS.Add_Click($checkboxClickHandler)


    # Remove checkbox
    $checkBox = New-Object System.Windows.Forms.CheckBox
    $checkBox.Checked = $config.autoremove
    $checkbox.Text = "Remove successfully copied files from source"
    $checkbox.TextAlign = "MiddleLeft"
    $checkbox.AutoSize = $true
    $checkBox.Location = New-Object System.Drawing.Point(12, 93)
    $form.Controls.Add($checkBox)


    # Copy button    
    $button = New-Object System.Windows.Forms.Button
    $button.Text = "Start Copy"
    $button.Size = New-Object System.Drawing.Size(100, 30)
    $button.Location = New-Object System.Drawing.Point(10, 123)
    $form.Controls.Add($button)

    $button.Add_Click({
            $selectedDrive = $comboBox.SelectedItem -split ':'
            if ($selectedDrive -eq "No drives found.") {
                [System.Windows.Forms.MessageBox]::Show("No drives found.")
                return
            }

            if ($selectedDrive) {
                $drive = "$($selectedDrive[0])"

                $sourcePath = "${drive}:\$($config.source)"
                $destinationPath = $config.destination
                $format = $null
                if ($checkboxFAT32.Checked) { $format = "FAT32" }
                if ($checkboxExFAT.Checked) { $format = "exFAT" }
                if ($checkboxNTFS.Checked) { $format = "NTFS" }
            
                # Start copying removing formatting etc
                CopyFiles -SourcePath "$sourcePath" -DestinationPath "$destinationPath" -Autoremove $checkBox.Checked -Format "$format" -Drive "$drive" -DriveDescription "$($comboBox.SelectedItem)"
            }
            else {
                [System.Windows.Forms.MessageBox]::Show("Please select a drive.")
            }

        })


    # settings icon
    $settingsPictureBox = New-Object System.Windows.Forms.PictureBox
    $settingsPictureBox.Size = New-Object System.Drawing.Size(25, 25)  # Adjust size as needed
    $settingsPictureBox.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::StretchImage
    $settingsPictureBox.Location = New-Object System.Drawing.Point(($form.ClientSize.Width - 16), ($form.ClientSize.Height - 16))  # Position in bottom right corner with margin
    $settingsPictureBox.Image = [System.Drawing.Icon]::ExtractAssociatedIcon("assets/settings.ico").ToBitmap()

    $settingsPictureBox.Add_Click({
            $configHash = (Get-FileHash "cameracopy.json" -Algorithm SHA256).Hash
            SettingsForm
            $newConfigHash = (Get-FileHash "cameracopy.json" -Algorithm SHA256).Hash
            if ($newConfigHash -ne $configHash) {
                $saveClose = $true
                $form.Close()
            }
        })

    $form.Controls.Add($settingsPictureBox)

    $form.add_FormClosing({
            if (-not $saveClose) {
                $global:runAgain = $false
            }
        })


    # Close splash and show main window
    $splash.Close()
    $form.ShowDialog()
}

# Run the program again if it is closed by saving settings save.
$global:runAgain = $true

while ($runAgain) {
    $drives, $splash, $config = SplashScan
    Main -Drives $drives -Config $config
}