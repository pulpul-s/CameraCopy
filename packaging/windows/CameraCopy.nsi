Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!ifndef APP_VERSION
  !error "APP_VERSION must be supplied with /DAPP_VERSION=x.y.z"
!endif
!ifndef APP_VERSION_NUM
  !error "APP_VERSION_NUM must be supplied with /DAPP_VERSION_NUM=x.y.z.0"
!endif
!ifndef APP_DIST
  !error "APP_DIST must be supplied with /DAPP_DIST=path"
!endif
!ifndef OUTPUT_DIR
  !error "OUTPUT_DIR must be supplied with /DOUTPUT_DIR=path"
!endif

!define APP_NAME "CameraCopy"
!define APP_PUBLISHER "CameraCopy"
!define APP_REG_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\CameraCopy"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUTPUT_DIR}\CameraCopy-Setup-${APP_VERSION}.exe"
InstallDir "$PROGRAMFILES64\CameraCopy"
RequestExecutionLevel admin
SetCompressor /SOLID lzma
VIProductVersion "${APP_VERSION_NUM}"
VIAddVersionKey /LANG=1033 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "FileDescription" "${APP_NAME} installer"
VIAddVersionKey /LANG=1033 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1033 "CompanyName" "${APP_PUBLISHER}"

!define MUI_ABORTWARNING
!define MUI_ICON "${__FILEDIR__}\..\..\cameracopy2\resources\icons\cameracopy.ico"
!define MUI_UNICON "${__FILEDIR__}\..\..\cameracopy2\resources\icons\cameracopy.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\CameraCopy.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Launch CameraCopy"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

Function .onInit
  SetRegView 64
  SetShellVarContext all
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "CameraCopy requires 64-bit Windows."
    Abort
  ${EndIf}
  FindWindow $0 "" "${APP_NAME}"
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "CameraCopy is running. Close it before installing or upgrading CameraCopy."
    Abort
  ${EndIf}
FunctionEnd

Function un.onInit
  SetRegView 64
  SetShellVarContext all
  FindWindow $0 "" "${APP_NAME}"
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "CameraCopy is running. Close it before uninstalling CameraCopy."
    Abort
  ${EndIf}
FunctionEnd

Section "CameraCopy application" SEC_APP
  SectionIn RO

  ; Move an existing installation aside before writing the new standalone tree.
  ; This keeps removed or renamed runtime files from surviving an upgrade.
  StrCpy $R0 "$INSTDIR.previous"
  RMDir /r "$R0"
  IfFileExists "$INSTDIR\*.*" 0 install_files
  ClearErrors
  Rename "$INSTDIR" "$R0"
  IfErrors 0 install_files
  MessageBox MB_ICONSTOP "CameraCopy could not replace the existing installation. Close CameraCopy and run the installer again."
  Abort

install_files:
  SetOutPath "$INSTDIR"
  File /r "${APP_DIST}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  RMDir /r /REBOOTOK "$R0"

  ; Remove shortcuts from a previous installation before optional sections
  ; recreate only those selected for this installation.
  Delete "$DESKTOP\CameraCopy.lnk"
  Delete "$SMPROGRAMS\CameraCopy\CameraCopy.lnk"
  Delete "$SMPROGRAMS\CameraCopy\Uninstall CameraCopy.lnk"
  RMDir "$SMPROGRAMS\CameraCopy"

  WriteRegStr HKLM "${APP_REG_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKLM "${APP_REG_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKLM "${APP_REG_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKLM "${APP_REG_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${APP_REG_KEY}" "DisplayIcon" "$INSTDIR\CameraCopy.exe"
  WriteRegStr HKLM "${APP_REG_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKLM "${APP_REG_KEY}" "NoModify" 1
  WriteRegDWORD HKLM "${APP_REG_KEY}" "NoRepair" 1
SectionEnd

Section "Start Menu shortcut" SEC_START_MENU
  CreateDirectory "$SMPROGRAMS\CameraCopy"
  CreateShortcut "$SMPROGRAMS\CameraCopy\CameraCopy.lnk" "$INSTDIR\CameraCopy.exe"
  CreateShortcut "$SMPROGRAMS\CameraCopy\Uninstall CameraCopy.lnk" "$INSTDIR\Uninstall.exe"
SectionEnd

Section /o "Desktop shortcut" SEC_DESKTOP
  CreateShortcut "$DESKTOP\CameraCopy.lnk" "$INSTDIR\CameraCopy.exe"
SectionEnd

Section "Uninstall"
  SetOutPath "$TEMP"
  Delete "$DESKTOP\CameraCopy.lnk"
  Delete "$SMPROGRAMS\CameraCopy\CameraCopy.lnk"
  Delete "$SMPROGRAMS\CameraCopy\Uninstall CameraCopy.lnk"
  RMDir "$SMPROGRAMS\CameraCopy"

  Delete "$INSTDIR\Uninstall.exe"
  RMDir /r "$INSTDIR"
  RMDir /r "$INSTDIR.previous"

  DeleteRegKey HKLM "${APP_REG_KEY}"
SectionEnd

LangString DESC_SEC_APP ${LANG_ENGLISH} "Install CameraCopy in Program Files."
LangString DESC_SEC_START_MENU ${LANG_ENGLISH} "Create CameraCopy shortcuts in the Start Menu."
LangString DESC_SEC_DESKTOP ${LANG_ENGLISH} "Create a CameraCopy shortcut on the desktop."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_APP} $(DESC_SEC_APP)
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_START_MENU} $(DESC_SEC_START_MENU)
  !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP} $(DESC_SEC_DESKTOP)
!insertmacro MUI_FUNCTION_DESCRIPTION_END
