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

echo [1.5/5] 重新生成内置提示词表
rem analyze.py 的注释说这一步是自动的——它必须真的是：改了 prompts/*.txt 忘了
rem 手跑 gen（2026-11-21 混剪提示词改版就差点发生），产物就带着旧提示词、无任何报错。
rem prompts/ 目录整个不在（被移走/改名）就当场失败，别打出一个空表的包。
if not exist "prompts\plans.txt" (
  echo [错误] 找不到 prompts\plans.txt，内置提示词表没法生成
  goto fail
)
"runtime\Scripts\python.exe" gen_builtin_prompts.py
if errorlevel 1 goto fail
"runtime\Scripts\python.exe" tests\test_builtin_prompts.py
if errorlevel 1 goto fail

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

echo [3/5] 组装可外部修改的资源（web / bin / models）
rem prompts 不再拷进产物：10 份提示词已按原字节内置进 app/_builtin_prompts.py。
rem 留明文在产物里 = 解压即读，藏了内置表也没意义（2026-11-22 防破解第一批）。
rem 开发期要调提示词：设 HOOK_DEV_PROMPTS=1 读源码目录的 prompts/，与打包产物无关。
robocopy "bin"     "%OUTDIR%\bin"     /MIR /NFL /NDL /NJH /NJS /NP >nul
rem web 先原样拷，再就地把 app.js / publish.js 用 terser 压缩（只 compress 不 mangle：
rem mangle 会改变量名、打坏 data-* 选择器和中文模板字符串；compress 不动这些，实测过）。
rem 源文件 web/ 保持明文不动，只压缩产物里的副本，开发期调试不受影响。
robocopy "web"     "%OUTDIR%\web"     /MIR /NFL /NDL /NJH /NJS /NP >nul
if exist "node_modules\.bin\terser.cmd" (
  echo   压缩前端 JS（只 compress）...
  call "node_modules\.bin\terser.cmd" "%OUTDIR%\web\app.js" -c -o "%OUTDIR%\web\app.js" >nul 2>&1
  call "node_modules\.bin\terser.cmd" "%OUTDIR%\web\publish.js" -c -o "%OUTDIR%\web\publish.js" >nul 2>&1
) else (
  echo   [警告] 没找到 terser，前端 JS 保持未压缩（不影响功能）
)
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

echo [4.5/5] 发布前断言扫描
rem 之前这步从来没被接入流程（2026-11-22 复查发现，且 GBK 控制台下 ✓ 号必崩
rem 导致单独跑也从未成功过）。现在打完包强制扫：源码/提示词明文、Key/secret
rem 泄露、data 运行残留，任一不过直接失败。
"runtime\Scripts\python.exe" check_release.py "%OUTDIR%"
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
