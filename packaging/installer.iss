; Inno Setup: установщик «Ячейка по штрихкоду».
; Собирать ПОСЛЕ PyInstaller, из корня проекта:
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
; Результат: dist\CellScanner-Setup-<версия>.exe

#define MyAppName "Ячейка по штрихкоду"
#define MyAppVersion "1.3.0"
; (1.3.0 — поле ручного ввода артикула под полем сканера: когда штрихкода на
;  товаре нет или он не читается. Enter — поиск строго по артикулу и печать)
; (1.2.2 — три исправления по итогам первой живой настройки:
;  * проверка токена спрашивала диск целиком и получала 403, хотя прав на папку
;    приложения достаточно — теперь проверяется сама папка;
;  * в поле токена можно вставлять весь адрес из браузера: токен выделяется сам,
;    а Ctrl+V в консоли (символ 0x16) распознаётся как неудавшаяся вставка;
;  * «файл базы не найден» больше не подменяет настоящую причину — показывается
;    ошибка скачивания. Плюс досыл не перезаливает уже загруженный файл)
; (1.2.1 — токен с кириллицей или пробелом больше не роняет приложение
;  UnicodeEncodeError: выдаётся понятное сообщение до отправки запроса)
; (1.2.0 — доставка файла через Яндекс.Диск: машинам не нужно видеть друг друга
;  по сети, сервер 1С не задействован. Общая папка осталась запасным вариантом.
;  Скачанный файл проверяется до подмены рабочей базы)
; (1.1.0 — работа от локальной базы: на складском ПК тонкий клиент 1С, в нём нет
;  comcntr.dll и COM невозможен. Машина с платформой выгружает срез в cells.db,
;  склад читает файл из общей папки. Прямой режим COM остался для выгрузки)
; (1.0.1 — самодиагностика окружения: 0x800401F3 «Недопустимая строка класса»
;  теперь называется своим именем — незарегистрированный COM-коннектор 1С;
;  добавлена кнопка «Подробности» и отчёт об окружении в журнале при запуске)
#define MyAppExe "CellScanner.exe"

[Setup]
; AppId фиксирован: по нему Windows опознаёт установку при обновлении версии.
AppId={{A7C3F1E2-5B84-4D6A-9E33-2F8B41C7D905}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Ячейка по штрихкоду
DefaultDirName={autopf}\CellScanner
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
; Приложение 64-битное (разрядность обязана совпадать с платформой 1С).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#SourcePath}..\dist
OutputBaseFilename=CellScanner-Setup-{#MyAppVersion}
SetupIconFile={#SourcePath}cellscanner.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; \
    GroupDescription: "Дополнительно:"
Name: "autostart"; Description: "Запускать при входе в Windows"; \
    GroupDescription: "Дополнительно:"; Flags: unchecked

; Каталог, куда PyInstaller положил сборку. Передаётся из build.ps1
; (ISCC /DDistDir=...), потому что прежний каталог бывает занят антивирусом и
; собирать приходится в новый. По умолчанию — обычный dist.
#ifndef DistDir
  #define DistDir "dist"
#endif

[Files]
Source: "{#SourcePath}..\{#DistDir}\CellScanner\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion
; Среда выполнения Microsoft Visual C++ — без неё Qt5/PySide2 на чистой Windows
; падает с «DLL load failed while importing QtCore». Ставится молча, после
; установки файл удаляется.
Source: "{#SourcePath}redist\vc_redist.x64.exe"; DestDir: "{tmp}"; \
    Flags: deleteafterinstall

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon
; Автозапуск — ярлыком в общей автозагрузке, а не записью в HKCU: установщик
; работает с правами администратора, и запись в HKCU легла бы в профиль того,
; чьими учётными данными подтвердили UAC, а не того, кто работает на складе.
Name: "{commonstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: autostart

[Run]
; Сначала среда выполнения VC++ (молча, без перезагрузки). Если такая же или
; более новая уже стоит, установщик это увидит и быстро завершится.
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; \
    StatusMsg: "Установка компонентов Microsoft Visual C++ (нужно один раз)…"; \
    Flags: waituntilterminated
Filename: "{app}\{#MyAppExe}"; Description: "Запустить {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

; Настройки и журнал остаются в %LOCALAPPDATA%\CellScanner и после удаления —
; при переустановке подключение к 1С и принтер настраивать заново не придётся.
