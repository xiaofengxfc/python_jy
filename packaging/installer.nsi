; biTrader Windows installer (NSIS 3)
;
; Build:
;   "%NSIS_DIR%\makensis.exe" packaging\installer.nsi
;
; Expects the PyInstaller output in dist\biTrader (produced by
; packaging\build_windows.bat) and writes dist\biTrader-Setup-<version>.exe.

Unicode true
SetCompressor /SOLID lzma

!define APP_NAME "biTrader"
!define APP_DISPLAY "biTrader · 币安交易工作台"
!define APP_VERSION "0.4.1"
!define APP_PUBLISHER "biTrader"
!define APP_EXE "biTrader.exe"
!define APP_DIR_NAME "biTrader"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\biTrader"

; Paths are resolved against the working directory, so makensis must run
; from the packaging\ directory (see packaging\build_windows.bat).
!define SOURCE_DIR "..\dist\biTrader"
!define ICON_FILE "bitrader.ico"

Name "${APP_DISPLAY}"
OutFile "..\dist\biTrader-Setup-${APP_VERSION}.exe"
; Per-user install: no UAC prompt, and every user can install without admin.
InstallDir "$LOCALAPPDATA\Programs\${APP_DIR_NAME}"
InstallDirRegKey HKCU "Software\${APP_NAME}" "InstallDir"
RequestExecutionLevel user
BrandingText "${APP_DISPLAY} v${APP_VERSION}"
ShowInstDetails nevershow
ShowUninstDetails nevershow

VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_DISPLAY} 安装程序"
VIAddVersionKey "FileVersion" "${APP_VERSION}.0"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "CompanyName" "${APP_PUBLISHER}"
VIAddVersionKey "LegalCopyright" "MIT License"

!include "MUI2.nsh"
!include "FileFunc.nsh"

!define MUI_ICON "${ICON_FILE}"
!define MUI_UNICON "${ICON_FILE}"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 ${APP_NAME}"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "在桌面创建快捷方式"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION CreateDesktopShortcut

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; --------------------------------------------------------------------- helpers
Function CreateDesktopShortcut
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
FunctionEnd

Function .onInit
  ; Refuse to install over a running instance: the exe file would be locked.
  nsExec::Exec 'taskkill /F /IM ${APP_EXE}'
  Pop $0
  SetShellVarContext current
FunctionEnd

Function un.onInit
  nsExec::Exec 'taskkill /F /IM ${APP_EXE}'
  Pop $0
  SetShellVarContext current
FunctionEnd

; ----------------------------------------------------------------------- pages
Section "主程序" SEC_MAIN
  SectionIn RO
  SetOutPath "$INSTDIR"
  SetOverwrite on

  File "${SOURCE_DIR}\${APP_EXE}"
  SetOutPath "$INSTDIR\_internal"
  File /r "${SOURCE_DIR}\_internal\*.*"

  SetOutPath "$INSTDIR"

  WriteRegStr HKCU "Software\${APP_NAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APP_NAME}" "Version" "${APP_VERSION}"

  ; Add/Remove Programs entry
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP_DISPLAY}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE},0"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" \
    "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk" \
    "$INSTDIR\Uninstall.exe" "" "$INSTDIR\Uninstall.exe" 0
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载 ${APP_NAME}.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\${APP_NAME}"

  ; User data (settings, SQLite database, K-line cache, plugins) is kept by
  ; default so reinstalling does not lose strategies or credentials.
  ; Silent uninstalls never prompt and always keep the data.
  IfSilent done
  IfFileExists "$APPDATA\${APP_DIR_NAME}\*.*" 0 done
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "是否同时删除用户数据？$\r$\n$\r$\n\
       $APPDATA\${APP_DIR_NAME}$\r$\n$\r$\n\
       包含设置、订单记录、K 线缓存与策略插件。$\r$\n\
       选择「否」将保留这些数据，以便重新安装后继续使用。" \
      IDYES delete_data IDNO done
  delete_data:
    RMDir /r "$APPDATA\${APP_DIR_NAME}"
  done:
SectionEnd
