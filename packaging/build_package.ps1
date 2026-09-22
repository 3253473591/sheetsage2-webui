<#
.SYNOPSIS
    Build the "SheetSage2 扒谱" distribution packages (full / lite).

.DESCRIPTION
    Full : portable CPython + all deps + both model checkpoints + ffmpeg + uv.
           Extract & double-click start.bat. No install, works offline.  ~5.8 GB
    Lite : app + uv.exe + ffmpeg only. start.bat triggers install.bat which
           downloads python / torch / weights on first run.           ~210 MB

    Both produce exactly the same layout, so start.bat / app code is shared.

.EXAMPLE
    pwsh -File packaging\build_package.ps1 -Mode Full
    pwsh -File packaging\build_package.ps1 -Mode Lite -SkipZip
#>
[CmdletBinding()]
param(
    [ValidateSet('Full', 'Lite')][string]$Mode = 'Full',
    [string]$Version = '1.0.0',
    [string]$DistRoot = '',
    [string]$ModelsSource = '',
    [string]$FfmpegSource = '',
    [string]$UvSource = '',
    [switch]$SkipZip,
    [switch]$SkipRuntime,
    [switch]$SkipModels,
    #: 扁平布局：包内容直接落在 dist\<full|lite>\ 下，不再套一层 SheetSage2-Transcribe-x.y.z\。
    #: 使用者（作者）习惯自己打 7z，扁平目录直接右键压缩最省事；此时不自动生成 zip。
    [switch]$Flat
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $DistRoot) { $DistRoot = Join-Path $ProjectRoot 'dist' }

# 本机资源（模型目录 / ffmpeg.exe）的默认值来自**不入库**的 local_paths.json，
# 这样仓库里不会出现作者机器的绝对路径。命令行显式传参优先。
# 别人克隆仓库后没有该文件，必须自己传 -ModelsSource / -FfmpegSource。
$LocalPathsFile = Join-Path $ProjectRoot 'local_paths.json'
$LocalPaths = @{}
if (Test-Path -LiteralPath $LocalPathsFile) {
    try {
        $LocalPaths = Get-Content -LiteralPath $LocalPathsFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Warning "local_paths.json 解析失败，忽略：$_"
        $LocalPaths = @{}
    }
}
if (-not $ModelsSource) { $ModelsSource = $LocalPaths.build_models_source }
if (-not $FfmpegSource) { $FfmpegSource = $LocalPaths.build_ffmpeg_source }
if (-not $ModelsSource) {
    throw "未指定模型目录。请传 -ModelsSource <dir>，或在项目根写 local_paths.json（见 local_paths.example.json）。"
}
#: zip 里看到的顶层文件夹名（两种形态保持一致，测试者看到的目录名一样）
$PkgName = "SheetSage2-Transcribe-$Version"
#: 实际工作目录按形态分开。**必须分开**：Full 与 Lite 用同一个 $OutDir 时，
#: 后构建的那个会把前一个整个删掉（实际踩过：全量包被 Lite 构建清空，
#: 恰好在跑的验收任务因此加载不到权重）。zip 仍从各自的 stage 目录打包，
#: 所以压缩包内部的顶层目录名是一样的。
$StageDir = Join-Path $DistRoot $Mode.ToLower()
$OutDir   = if ($Flat) { $StageDir } else { Join-Path $StageDir $PkgName }

# 轻量包**不带权重**（也不带便携 Python）：由 start.bat -> install.bat ->
# tools\install_deps.py 在测试者机器上首次运行时下载。
# 实际踩过：忘了这一步，Lite 包被打成 2.90 GB（含 2.57 GB 权重）。
if ($Mode -eq 'Lite' -and -not $SkipModels) {
    Write-Host "  [lite] model weights are NOT bundled - install_deps.py fetches them on first run"
    $SkipModels = $true
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
    Write-Host "  $Message" -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
}

function Copy-Tree {
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$Destination,
        [string[]]$ExcludeDirs = @(),
        [string[]]$ExcludeFiles = @()
    )
    if (-not (Test-Path -LiteralPath $Source)) { throw "source missing: $Source" }
    $null = New-Item -ItemType Directory -Force -Path $Destination
    $robo = @($Source, $Destination, '/E', '/MT:16', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
    if ($ExcludeDirs.Count)  { $robo += '/XD'; $robo += $ExcludeDirs }
    if ($ExcludeFiles.Count) { $robo += '/XF'; $robo += $ExcludeFiles }
    & robocopy @robo | Out-Null
    # robocopy: 0-7 are success codes, >=8 means failure
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE): $Source -> $Destination" }
    $global:LASTEXITCODE = 0
}

function Get-TreeSize {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    return (Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
}

function Format-GB([double]$Bytes) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }

# --------------------------------------------------------------------------
# sanity checks
# --------------------------------------------------------------------------
Write-Step "Preflight ($Mode)"

$VenvCfg = Join-Path $ProjectRoot '.venv\pyvenv.cfg'
$BasePython = $null
if (Test-Path -LiteralPath $VenvCfg) {
    $line = (Get-Content -LiteralPath $VenvCfg | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1)
    if ($line) { $BasePython = ($line -split '=', 2)[1].Trim() }
}
if (-not $BasePython -or -not (Test-Path -LiteralPath (Join-Path $BasePython 'python.exe'))) {
    throw "cannot locate the base CPython (looked at .venv\pyvenv.cfg -> '$BasePython')"
}

$SitePackages = Join-Path $ProjectRoot '.venv\Lib\site-packages'
if (-not (Test-Path -LiteralPath $SitePackages)) { throw "missing $SitePackages" }

if (-not $UvSource) {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { $UvSource = $cmd.Source }
}

$NeedWeights = (-not $SkipModels)
foreach ($need in @(
    @{ p = (Join-Path $ModelsSource 'SheetSage2\model.safetensors'); what = 'SheetSage2 weights' },
    @{ p = (Join-Path $ModelsSource 'MERT-v2-FullSong\model.safetensors'); what = 'MERT weights' }
)) {
    if ($NeedWeights -and -not (Test-Path -LiteralPath $need.p)) { throw "missing $($need.what): $($need.p)" }
}
if (-not (Test-Path -LiteralPath $FfmpegSource)) { throw "missing ffmpeg: $FfmpegSource" }

Write-Host "  project      : $ProjectRoot"
Write-Host "  base python  : $BasePython"
Write-Host "  site-packages: $SitePackages"
Write-Host "  models       : $ModelsSource"
Write-Host "  ffmpeg       : $FfmpegSource"
Write-Host "  uv           : $(if ($UvSource) { $UvSource } else { '<not found - lite package will lack uv>' })"
Write-Host "  output       : $OutDir"

# --------------------------------------------------------------------------
# 0. clean output
# --------------------------------------------------------------------------
Write-Step "0/6  Clean output directory"
if (Test-Path -LiteralPath $OutDir) { Remove-Item -LiteralPath $OutDir -Recurse -Force }
$null = New-Item -ItemType Directory -Force -Path $OutDir

# --------------------------------------------------------------------------
# 1. application payload
# --------------------------------------------------------------------------
Write-Step "1/6  Application payload"
Copy-Tree -Source (Join-Path $ProjectRoot 'app') -Destination (Join-Path $OutDir 'app') `
          -ExcludeDirs @('__pycache__')
Copy-Tree -Source (Join-Path $ProjectRoot 'web') -Destination (Join-Path $OutDir 'web')

Copy-Item -LiteralPath (Join-Path $ProjectRoot 'requirements-base.txt') -Destination $OutDir -Force
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'requirements.txt')      -Destination (Join-Path $OutDir 'requirements-full-reference.txt') -Force

foreach ($tpl in @('start.bat', 'install.bat', 'selfcheck.bat')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "templates\$tpl") -Destination $OutDir -Force
}
Copy-Tree -Source (Join-Path $PSScriptRoot 'tools') -Destination (Join-Path $OutDir 'tools') -ExcludeDirs @('__pycache__')

foreach ($doc in @('使用说明.md', 'ASK_AI.md', '许可与来源.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "docs\$doc") -Destination $OutDir -Force
}

# torch pin files, for humans / AI assistants reading the folder
@(
    "# GPU (NVIDIA, CUDA 13.0) - install with:",
    "#   runtime\python\python.exe -m pip install -r requirements-torch-cu130.txt --index-url https://download.pytorch.org/whl/cu130",
    "# NOTE: minimum supported torch is 2.2 (recommended >= 2.6); torchaudio must match torch exactly.",
    "#       This index only carries torch >= 2.9. For older CUDA builds use .../cu124, cu126 or cu128.",
    "#       See the PyTorch section in 使用说明.md / ASK_AI.md.",
    "torch==2.10.0+cu130",
    "torchaudio==2.10.0+cu130"
) | Set-Content -LiteralPath (Join-Path $OutDir 'requirements-torch-cu130.txt') -Encoding ascii
@(
    "# CPU only - install with:",
    "#   runtime\python\python.exe -m pip install -r requirements-torch-cpu.txt --index-url https://download.pytorch.org/whl/cpu",
    "# NOTE: minimum supported torch is 2.2 (recommended >= 2.6); torchaudio must match torch exactly.",
    "#       See the PyTorch section in 使用说明.md / ASK_AI.md.",
    "torch==2.10.0+cpu",
    "torchaudio==2.10.0+cpu"
) | Set-Content -LiteralPath (Join-Path $OutDir 'requirements-torch-cpu.txt') -Encoding ascii

# runtime dirs that the app expects to exist (config.ensure_dirs also creates them)
$null = New-Item -ItemType Directory -Force -Path (Join-Path $OutDir 'output')
$null = New-Item -ItemType Directory -Force -Path (Join-Path $OutDir 'tmp')

# Ship a real settings.json so testers have something to edit (e.g. force CPU).
# NOTE: deliberately no "output_dir" key - leaving it out lets the app compute it
# from its own location, which is what keeps the package relocatable.
$Settings = [ordered]@{
    default_device           = 'auto'
    default_dtype            = 'bf16'
    default_preset           = 'default'
    default_only_melody      = $true
    default_drop_intro_outro = $false
    default_max_seconds      = $null
    theme                    = 'light'
    port                     = 8777
    max_upload_mb            = 300
    open_browser             = $true
}
$Settings | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $OutDir 'settings.json') -Encoding utf8

# --------------------------------------------------------------------------
# 2. licenses
# --------------------------------------------------------------------------
Write-Step "2/6  Licenses and attribution"
$LicDir = Join-Path $OutDir 'licenses'
$null = New-Item -ItemType Directory -Force -Path $LicDir
Copy-Item -LiteralPath (Join-Path $ModelsSource 'SheetSage2\LICENSE.txt')              -Destination (Join-Path $LicDir 'SheetSage2-LICENSE.txt') -Force
Copy-Item -LiteralPath (Join-Path $ModelsSource 'SheetSage2\THIRD_PARTY_NOTICES.md')   -Destination (Join-Path $LicDir 'SheetSage2-THIRD_PARTY_NOTICES.md') -Force
Copy-Item -LiteralPath (Join-Path $ModelsSource 'MERT-v2-FullSong\LICENSE.txt')        -Destination (Join-Path $LicDir 'MERT-v2-FullSong-LICENSE.txt') -Force
Copy-Item -LiteralPath (Join-Path $ModelsSource 'MERT-v2-FullSong\THIRD_PARTY_NOTICES.md') -Destination (Join-Path $LicDir 'MERT-v2-FullSong-THIRD_PARTY_NOTICES.md') -Force
Copy-Tree -Source (Join-Path $PSScriptRoot 'licenses') -Destination $LicDir
Copy-Item -LiteralPath (Join-Path $ProjectRoot 'web\vendor\soundfont\ATTRIBUTION.md') -Destination (Join-Path $LicDir 'FluidR3-ATTRIBUTION.md') -Force

# --------------------------------------------------------------------------
# 3. portable runtime
# --------------------------------------------------------------------------
if (-not $SkipRuntime) {
    Write-Step "3/6  Portable Python runtime + ffmpeg"

    if ($Mode -eq 'Full') {
        $RuntimePython = Join-Path $OutDir 'runtime\python'
        Write-Host "  copying base CPython ..."
        Copy-Tree -Source $BasePython -Destination $RuntimePython
        Write-Host "  copying site-packages from .venv ..."
        Copy-Tree -Source $SitePackages -Destination (Join-Path $RuntimePython 'Lib\site-packages') `
                  -ExcludeDirs @('__pycache__') -ExcludeFiles @('*.pyc', '*.pyo')
        Write-Host "  removing uv's externally-managed marker (so pip works) ..."
        foreach ($marker in @(
            (Join-Path $RuntimePython 'Lib\EXTERNALLY-MANAGED'),
            (Join-Path $RuntimePython 'Lib\site-packages\EXTERNALLY-MANAGED')
        )) {
            if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force }
        }
        Write-Host "  python runtime size: $(Format-GB (Get-TreeSize $RuntimePython))"
    }

    if ($UvSource -and (Test-Path -LiteralPath $UvSource)) {
        $null = New-Item -ItemType Directory -Force -Path (Join-Path $OutDir 'runtime')
        Copy-Item -LiteralPath $UvSource -Destination (Join-Path $OutDir 'runtime\uv.exe') -Force
        Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $UvSource) 'uvx.exe') -Destination (Join-Path $OutDir 'runtime\uvx.exe') -Force -ErrorAction SilentlyContinue
    }

    $FfmpegDir = Join-Path $OutDir 'runtime\ffmpeg\bin'
    $null = New-Item -ItemType Directory -Force -Path $FfmpegDir
    Copy-Item -LiteralPath $FfmpegSource -Destination (Join-Path $FfmpegDir 'ffmpeg.exe') -Force
    Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $FfmpegSource) 'ffprobe.exe') -Destination (Join-Path $FfmpegDir 'ffprobe.exe') -Force -ErrorAction SilentlyContinue
    Write-Host "  ffmpeg bundled"
}

# --------------------------------------------------------------------------
# 4. model weights
# --------------------------------------------------------------------------
if (-not $SkipModels) {
    Write-Step "4/6  Model weights (this is the slow part, ~2.6 GB)"
    Copy-Tree -Source (Join-Path $ModelsSource 'SheetSage2')        -Destination (Join-Path $OutDir 'models\SheetSage2') `
              -ExcludeDirs @('__pycache__')
    Copy-Tree -Source (Join-Path $ModelsSource 'MERT-v2-FullSong')  -Destination (Join-Path $OutDir 'models\MERT-v2-FullSong') `
              -ExcludeDirs @('__pycache__')
    Write-Host "  models size: $(Format-GB (Get-TreeSize (Join-Path $OutDir 'models')))"
}

# --------------------------------------------------------------------------
# 5. version manifest
# --------------------------------------------------------------------------
Write-Step "5/6  Manifest (版本信息.json)"
$TorchVersion = 'unknown'
$PyVersion = 'unknown'
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $VenvPython) {
    $PyVersion = (& $VenvPython -c "import sys;print(sys.version.split()[0])" 2>$null | Select-Object -First 1)
    $TorchVersion = (& $VenvPython -c "import torch;print(torch.__version__)" 2>$null | Select-Object -First 1)
}

function Get-Sha256([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

$Manifest = [ordered]@{
    name           = 'SheetSage2 扒谱 (SheetSage2 Transcribe)'
    version        = $Version
    edition        = $(if ($Mode -eq 'Full') { 'full' } else { 'lite' })
    built_at       = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    built_on       = "$env:COMPUTERNAME / $([System.Environment]::OSVersion.VersionString)"
    build_host_gpu = 'NVIDIA GeForce RTX 3080 12GB (reference machine)'
    python         = $PyVersion
    torch          = $TorchVersion
    start_here     = 'start.bat'
    notes          = @(
        'Full edition: portable runtime + weights included, works offline.',
        'Lite edition: run start.bat once with internet to fetch runtime + weights.',
        'Model weights are CC BY-NC 4.0 (non-commercial). See 许可与来源.md.'
    )
    weights        = [ordered]@{
        'SheetSage2/model.safetensors'        = @{ bytes = (Get-Item -LiteralPath (Join-Path $ModelsSource 'SheetSage2\model.safetensors')).Length; sha256 = (Get-Sha256 (Join-Path $OutDir 'models\SheetSage2\model.safetensors')) }
        'MERT-v2-FullSong/model.safetensors'  = @{ bytes = (Get-Item -LiteralPath (Join-Path $ModelsSource 'MERT-v2-FullSong\model.safetensors')).Length; sha256 = (Get-Sha256 (Join-Path $OutDir 'models\MERT-v2-FullSong\model.safetensors')) }
    }
}
$Manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutDir '版本信息.json') -Encoding utf8

# --------------------------------------------------------------------------
# 6. summary / package
# --------------------------------------------------------------------------
Write-Step "6/6  Package"
$TotalSize = Get-TreeSize $OutDir
$FileCount = (Get-ChildItem -LiteralPath $OutDir -Recurse -File).Count
Write-Host "  payload : $(Format-GB $TotalSize)  ($FileCount files)"

if ($Flat) {
    Write-Host "  flat layout -> $OutDir  (no zip; pack it yourself, e.g. 7z)"
} elseif (-not $SkipZip) {
    # 用 Python 的 zipfile 而不是系统 tar.exe：bsdtar 写 zip 时不置
    # UTF-8 文件名标志位（bit 11），中文文件名会被写成 ANSI 代码页，
    # 换个解压工具就变乱码。详见 packaging\make_zip.py 的注释。
    $ZipPath = Join-Path $StageDir "$PkgName-$($Mode.ToLower()).zip"
    $ZipPy   = Join-Path $BasePython 'python.exe'
    Write-Host "  zipping -> $ZipPath  (python zipfile, UTF-8 entry names)"
    & $ZipPy -X utf8 (Join-Path $PSScriptRoot 'make_zip.py') $StageDir $PkgName --out $ZipPath
    if ($LASTEXITCODE -ne 0) { throw "make_zip.py failed ($LASTEXITCODE)" }
    Write-Host "  zip     : $(Format-GB (Get-Item -LiteralPath $ZipPath).Length)"
    Get-Content -LiteralPath "$ZipPath.sha256.txt" | ForEach-Object { Write-Host "  sha256  : $_" }
}

Write-Host ""
Write-Host "DONE. Output folder: $OutDir" -ForegroundColor Green
