; Inno Setup 6 script for MixCut Studio (Windows installer).
;
; Compile with the ISCC bundled into the workspace toolchain:
;   _tools\InnoSetup\ISCC.exe build_tools\mixcut-setup.iss
;
; Relative paths resolve against this file's directory, so SourceDir/DocsDir point
; back at the project root while OutputDir collects the finished Setup.exe.

#define AppName "MixCut Studio"
#define AppNameCn "MixCut Studio 本地批量混剪工具"
#define AppVersion "1.0.0"
#define AppPublisher "waltertech"
#define AppURL "https://github.com/waltertech/mixcut-studio"
#define AppExe "MixCutStudio.exe"
#define SourceDir "..\dist\MixCutStudio"
#define DocsDir "..\src_pkg\mixcut-studio-main"

[Setup]
AppId={{8F1B7C2A-4D3E-4A6B-9C7D-2E5F8A1B3C4D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppNameCn} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppNameCn} 安装程序
VersionInfoCompany={#AppPublisher}
; Per-user install by default: no UAC prompt, and the app already keeps its data
; under %LOCALAPPDATA%.  Pass /ALLUSERS on the command line for a machine-wide install.
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=no
AllowNoIcons=yes
LicenseFile={#DocsDir}\README.md
OutputDir=..\output
OutputBaseFilename=MixCutStudio-{#AppVersion}-win64-Setup
SetupIconFile=mixcut.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppNameCn}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
UsePreviousAppDir=yes
UsePreviousGroup=yes
CloseApplications=no
RestartApplications=no
DisableDirPage=no
DisableWelcomePage=no
ShowLanguageDialog=auto
Uninstallable=yes

[Languages]
Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinese.CreateDesktopIcon=创建桌面快捷方式
chinese.AdditionalIcons=附加快捷方式：
chinese.LaunchProgram=立即启动 {#AppName}
chinese.StopShortcut=停止后台服务
chinese.RunHint=双击图标即可启动。服务在后台运行，关闭浏览器不会中断导出任务。
chinese.DataHint=素材目录、导出记录与配置保存在：%1%n卸载时可以选择是否一并删除。
chinese.CloseRunning=检测到 {#AppName} 正在运行。安装/卸载前需要先停止后台服务，是否立即停止？
chinese.KeepData=是否同时删除用户数据？%n%n包括素材目录、导出记录、贴图库和配置，位于：%n%1%n%n选择“否”将保留这些数据，便于以后重新安装后继续使用。
chinese.StopFailed=无法停止后台服务，请先在任务管理器中结束 {#AppExe} 后重试。
english.CreateDesktopIcon=Create a &desktop shortcut
english.AdditionalIcons=Additional shortcuts:
english.LaunchProgram=Launch {#AppName} now
english.StopShortcut=Stop background service
english.RunHint=Double-click the icon to start. The service keeps running after the browser closes.
english.DataHint=Material folders, exports and settings live in:%n%1%nYou can remove them during uninstall.
english.CloseRunning={#AppName} is running. The background service must be stopped first. Stop it now?
english.KeepData=Delete user data as well?%n%nThis removes material folders, exports, stickers and settings from:%n%1%n%nChoose No to keep them for a future reinstall.
english.StopFailed=Could not stop the background service. End {#AppExe} in Task Manager and try again.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#DocsDir}\README.md"; DestDir: "{app}"; DestName: "使用说明.md"; Flags: ignoreversion
Source: "{#DocsDir}\PROJECT_PLAN.md"; DestDir: "{app}"; DestName: "项目说明.md"; Flags: ignoreversion
Source: "{#DocsDir}\VALIDATION.md"; DestDir: "{app}"; DestName: "验收记录.md"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "{cm:RunHint}"
Name: "{group}\{cm:StopShortcut}"; Filename: "{app}\{#AppExe}"; Parameters: "--stop"; WorkingDir: "{app}"
Name: "{group}\使用说明"; Filename: "{app}\使用说明.md"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#AppExe}"; Parameters: "--stop"; RunOnceId: "StopService"; Flags: runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#AppExe}"

[Code]
const
  DataDirName = 'MixCutStudio';

function UserDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\' + DataDirName);
end;

function ServiceExe(): String;
begin
  Result := ExpandConstant('{app}\{#AppExe}');
end;

{ Returns True while a process with our image name is still running.
  tasklist exits 0 even when nothing matches, so the output has to be inspected. }
function ProcessAlive(): Boolean;
var
  ResultCode: Integer;
  Output: AnsiString;
  TempFile: String;
begin
  TempFile := ExpandConstant('{tmp}\mixcut-tasklist.txt');
  DeleteFile(TempFile);
  Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq {#AppExe}" /NH > "' + TempFile + '"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Output := '';
  LoadStringFromFile(TempFile, Output);
  DeleteFile(TempFile);
  Result := Pos(LowerCase('{#AppExe}'), LowerCase(String(Output))) > 0;
end;

{ Ask the running service to stop, then force-kill any leftover process so the
  bundle's files are unlocked before install/uninstall touches them.
  Returns True when a process is still alive afterwards. }
function StopRunningService(): Boolean;
var
  ResultCode: Integer;
  Attempt: Integer;
begin
  if FileExists(ServiceExe()) then
    Exec(ServiceExe(), '--stop', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := True;
  for Attempt := 1 to 10 do
  begin
    if not ProcessAlive() then
    begin
      Result := False;
      Exit;
    end;
    Exec('taskkill.exe', '/F /IM {#AppExe} /T', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    Sleep(500);
    if not ProcessAlive() then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if StopRunningService() then
    Result := CustomMessage('StopFailed');
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  { WizardSilent must not be called from the uninstaller; UninstallSilent is the
    equivalent query in this phase. }
  if not UninstallSilent then
    if MsgBox(CustomMessage('CloseRunning'), mbConfirmation, MB_YESNO) = IDYES then
      Result := not StopRunningService();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WizardSilent then
    MsgBox(FmtMessage(CustomMessage('DataHint'), [UserDataDir()]), mbInformation, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataDir := UserDataDir();
  if not DirExists(DataDir) then
    Exit;
  if UninstallSilent then
    Exit;
  if MsgBox(FmtMessage(CustomMessage('KeepData'), [DataDir]),
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(DataDir, True, True, True);
end;
