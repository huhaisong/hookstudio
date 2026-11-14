@echo off
chcp 65001 >nul
title 钩子工坊 · 打包
cd /d "%~dp0"

set APPNAME=HookStudio
set OUTDIR=dist\%APPNAME%
set ZIPFLAG=%1

if not exist "runtime\Scripts\python.exe" (
  echo [错误] 找不到 runtime\Scripts\python.exe，先建好虚拟环境再打包
  pause
  exit /b 1
)

echo [1/5] 清理旧产物
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [2/5] PyInstaller 打包（onedir，外部资源不封进去）
rem --noupx 不是可选项：UPX 压缩 onnxruntime / ctranslate2 的 .pyd 有已知的解压崩溃
rem 案例（转写一跑就 0xC0000005），体积只省几十 MB，不值得拿稳定性换。
"runtime\Scripts\python.exe" -m PyInstaller run.py ^
  --name %APPNAME% --onedir --console --noconfirm --clean --noupx ^
  --collect-submodules app ^
  --collect-submodules uvicorn ^
  --collect-all faster_whisper ^
  --collect-all ctranslate2 ^
  --collect-all onnxruntime ^
  --hidden-import app.main ^
  --exclude-module torch ^
  --exclude-module torchaudio ^
  --exclude-module transformers ^
  --exclude-module scipy ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module PySide6 ^
  --exclude-module tkinter
if errorlevel 1 goto fail

echo [3/5] 组装可外部修改的资源（web / prompts / models / bin）
robocopy "web"     "%OUTDIR%\web"     /MIR /NFL /NDL /NJH /NJS /NP >nul
robocopy "prompts" "%OUTDIR%\prompts" /MIR /NFL /NDL /NJH /NJS /NP >nul
robocopy "bin"     "%OUTDIR%\bin"     /MIR /NFL /NDL /NJH /NJS /NP >nul
rem 只带 small，base 永不进包（省约 140MB）
robocopy "models"  "%OUTDIR%\models"  /MIR /XD "%CD%\models\base" /NFL /NDL /NJH /NJS /NP >nul

echo [4/5] 放空的 data 目录 + 分发模式配置 + 使用说明
rem 具体动作都在 build_post.py 里：写 dist 的 config.json（只写 dist_mode/report_audit，
rem 绝不拷开发机那份带明文 Key 的）、拷使用说明、打体积摘要。
rem 为什么不在 bat 里写 python -c "...长命令..."：2026-11-04 实测 cmd 会把长命令里
rem 的引号和中文拆坏，冒出 'sys' is not recognized 但退出码仍是 0，看着像成功其实没跑。
if not exist "%OUTDIR%\data" mkdir "%OUTDIR%\data"
"runtime\Scripts\python.exe" build_post.py "%OUTDIR%"
if errorlevel 1 goto fail

echo [5/5] 把 exe 改成中文名
"runtime\Scripts\python.exe" -c "import os;d=os.path.abspath(r'%OUTDIR%');a=os.path.join(d,'%APPNAME%.exe');b=os.path.join(d,'\u94a9\u5b50\u5de5\u574a.exe');os.replace(a,b) if os.path.exists(a) else None"

echo.
echo 打包完成：%OUTDIR%
echo 双击里面的 钩子工坊.exe 即可运行。

if /i "%ZIPFLAG%"=="zip" (
  echo.
  echo 正在压缩发布包（体积较大，需要几分钟）...
  rem 必须走 pack_zip.py：它会先重置分发配置再压。直接用 Compress-Archive 会踩坑——
  rem 打完包为了验证双击跑一次 exe，程序会把带 jwt_secret 的完整 config 写回 data\，
  rem 再压缩就把这个密钥发给所有用户了（2026-11-04 实际发生过）。
  "runtime\Scripts\python.exe" pack_zip.py "%OUTDIR%" "dist\钩子工坊.zip"
  if errorlevel 1 goto fail
  echo 已生成：dist\钩子工坊.zip
)
pause
exit /b 0

:fail
echo.
echo [失败] 打包出错，看上面的报错信息
pause
exit /b 1
