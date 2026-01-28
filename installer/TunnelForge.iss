; TunnelForge - Inno Setup Script
; Windows Installer 생성을 위한 스크립트
;
; 빌드 방법:
;   1. Inno Setup 6 설치: https://jrsoftware.org/isinfo.php
;   2. 명령줄: ISCC.exe installer\TunnelForge.iss
;   3. 또는: .\scripts\build-installer.ps1

#define MyAppName "TunnelForge"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "sanghyun-io"
#define MyAppURL "https://github.com/sanghyun-io/tunnelforge"
#define MyAppExeName "TunnelForge.exe"

[Setup]
; 애플리케이션 기본 정보
AppId={{A5B3C8D2-1F4E-4A9B-8C7D-2E5F6A3B4C1D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; 설치 경로 설정
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

; 권한 설정 (lowest: 관리자 권한 불필요)
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

; 라이선스 파일 (.iss 파일 기준 상대 경로)
LicenseFile=LICENSE.txt

; 출력 설정 (프로젝트 루트의 output 폴더)
OutputDir=..\output
OutputBaseFilename=TunnelForge-Setup-{#MyAppVersion}

; 압축 설정
Compression=lzma2/ultra64
SolidCompression=yes

; UI 설정
WizardStyle=modern
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

; 아키텍처 설정
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 실행 중인 애플리케이션 자동 종료
CloseApplications=force
CloseApplicationsFilter=*.exe
RestartApplications=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 메인 실행 파일 (.iss 파일 기준 상대 경로)
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion restartreplace

; 복구/업데이트 프로그램 (부트스트래퍼)
Source: "..\dist\TunnelForge-WebSetup.exe"; DestDir: "{app}"; Flags: ignoreversion

; 라이선스 파일 포함 (선택적)
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

; NOTE: Don't use "Flags: ignoreversion" on any shared system files

[Icons]
; 시작 메뉴 단축키
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\복구 및 업데이트"; Filename: "{app}\TunnelForge-WebSetup.exe"; Comment: "최신 버전으로 재설치"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"

; 바탕화면 아이콘 (선택 사항)
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 설치 완료 후 프로그램 실행 옵션
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
/////////////////////////////////////////////////////////////////////
// 이전 버전 자동 제거
/////////////////////////////////////////////////////////////////////
function GetUninstallString(): String;
var
  sUnInstPath: String;
  sUnInstallString: String;
begin
  sUnInstPath := ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{#emit SetupSetting("AppId")}_is1');
  sUnInstallString := '';
  if not RegQueryStringValue(HKLM, sUnInstPath, 'UninstallString', sUnInstallString) then
    RegQueryStringValue(HKCU, sUnInstPath, 'UninstallString', sUnInstallString);
  Result := sUnInstallString;
end;

/////////////////////////////////////////////////////////////////////
function IsUpgrade(): Boolean;
begin
  Result := (GetUninstallString() <> '');
end;

/////////////////////////////////////////////////////////////////////
function UnInstallOldVersion(): Integer;
var
  sUnInstallString: String;
  iResultCode: Integer;
begin
  // Return Values:
  // 1 - uninstall string is empty
  // 2 - error executing the UnInstallString
  // 3 - successfully executed the UnInstallString

  // default return value
  Result := 0;

  // get the uninstall string of the old app
  sUnInstallString := GetUninstallString();
  if sUnInstallString <> '' then begin
    sUnInstallString := RemoveQuotes(sUnInstallString);
    if Exec(sUnInstallString, '/SILENT /NORESTART /SUPPRESSMSGBOXES','', SW_HIDE, ewWaitUntilTerminated, iResultCode) then
      Result := 3
    else
      Result := 2;
  end else
    Result := 1;
end;

/////////////////////////////////////////////////////////////////////
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep=ssInstall) then
  begin
    if (IsUpgrade()) then
    begin
      UnInstallOldVersion();
    end;
  end;
end;
