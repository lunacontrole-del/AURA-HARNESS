#requires -Version 5.1
<#
AURA QUANT-X Unified — AURA_MONITOR_FIXED_v3.ps1
Versão: 12.6.17-monitor-v4-audit-max
Objetivo: monitoramento operacional, diagnóstico e auditoria READ-ONLY do AURA em C:\aura.

Base preservada da V2:
- serviços/portas
- TCP + HTTP health
- processos AURA/IA
- runtime
- auditoria de logs

Cobertura ampliada conforme MANUAL.md v12.6.17:
- Bridge 8080
- Engine 8765
- Voice 8099
- Ollama 11434
- endpoints de status/telemetria/analysis/observability/orchestrator
- modelo Ollama e fallback
- GPU/CUDA/Torch (somente leitura)
- Python/venv/imports (somente leitura)
- persistência e arquivos críticos
- freshness do feed e runtime
- runtime_events.jsonl
- diagnósticos JSON/TXT existentes
- extensão e Manifest
- firewall/Defender/DISM/SFC (somente leitura, quando disponível)
- reconciliação de escanteios quando dados recentes existirem
- diagnóstico de gates/decisão/risk/data_integrity quando a API expuser esses campos
- identificação de serviços críticos vs opcionais
- snapshot consolidado e log de auditoria local do próprio monitor

IMPORTANTE:
Este script NÃO mata processos, NÃO inicia serviços, NÃO libera portas, NÃO altera firewall,
NÃO altera banco, NÃO executa DISM/SFC com alteração e NÃO escreve no sistema AURA, exceto
um pequeno log local opcional do próprio monitor em halem_control\monitor_v3_audit.jsonl.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

# ================================================================
# CONFIGURAÇÃO CENTRAL
# ================================================================
$ScriptVersion = '12.6.17-monitor-v4-audit-max'
$Root = 'C:\aura'
$RefreshSeconds = 3
$HttpTimeoutSeconds = 3
$TcpTimeoutMs = 900
$MaxLogLinesPerFile = 250
$MaxErrorsShown = 20
$EnableSelfAuditLog = $true
$SelfAuditLog = Join-Path $Root 'halem_control\monitor_v3_audit.jsonl'
$DiagnosticsDir = Join-Path $Root 'diagnostics'
$RuntimeDir = Join-Path $Root 'halem_control\runtime'
$EngineDir = Join-Path $Root 'engine'
$BridgeDir = Join-Path $Root 'bridge'
$ExtensionDir = Join-Path $Root 'extensao'
$JarvisDir = Join-Path $BridgeDir 'jarvis'

# Limites conservadores apenas para classificação visual.
$FeedStaleSeconds = 45
$WarningDiskFreeGB = 10
$WarningMemoryFreePercent = 15
$MaxHealthLatencyMs = 1500

# Serviços oficiais do manual.
$Services = @(
    [pscustomobject]@{ Name='OLLAMA'; Port=11434; PrimaryUrl='http://127.0.0.1:11434/api/tags'; FallbackUrls=@('http://127.0.0.1:11434/'); Critical=$true; Role='LLM local' },
    [pscustomobject]@{ Name='ENGINE'; Port=8765; PrimaryUrl='http://127.0.0.1:8765/health'; FallbackUrls=@('http://127.0.0.1:8765/api/status'); Critical=$true; Role='IA + risco + feedback' },
    [pscustomobject]@{ Name='BRIDGE'; Port=8080; PrimaryUrl='http://127.0.0.1:8080/health'; FallbackUrls=@(); Critical=$true; Role='Feed + REG' },
    [pscustomobject]@{ Name='VOICE'; Port=8099; PrimaryUrl='http://127.0.0.1:8099/api/voice/health'; FallbackUrls=@('http://127.0.0.1:8099/api/health'); Critical=$true; Role='STT + LLM + TTS' }
)

# Endpoints documentados. São consultados somente quando o serviço-base responde.
$EngineEndpoints = @(
    @{ Name='ENGINE /api/status'; Url='http://127.0.0.1:8765/api/status'; Method='GET' },
    @{ Name='ENGINE /api/orchestrator/status'; Url='http://127.0.0.1:8765/api/orchestrator/status'; Method='GET' },
    @{ Name='ENGINE /api/observability/events'; Url='http://127.0.0.1:8765/api/observability/events'; Method='GET' }
)

$BridgeEndpoints = @(
    @{ Name='BRIDGE /health'; Url='http://127.0.0.1:8080/health'; Method='GET' }
)

$VoiceEndpoints = @(
    @{ Name='VOICE /api/voice/health'; Url='http://127.0.0.1:8099/api/voice/health'; Method='GET' },
    @{ Name='VOICE /api/voice/diagnostic'; Url='http://127.0.0.1:8099/api/voice/diagnostic'; Method='GET' }
)

# Arquivos de runtime/logs citados no manual.
$OfficialLogs = @(
    (Join-Path $EngineDir 'runtime_engine.log'),
    (Join-Path $BridgeDir 'runtime_bridge.log'),
    (Join-Path $BridgeDir 'runtime_voice.log'),
    (Join-Path $Root 'install_run.log'),
    (Join-Path $Root 'recovery_services.log'),
    (Join-Path $Root 'validacao_sistema.log'),
    (Join-Path $BridgeDir 'AURA_DIAGNOSTIC_REPORT.txt')
)

$CriticalFiles = @(
    @{ Label='MANUAL.md'; Path=(Join-Path $Root 'MANUAL.md'); Severity='INFO' },
    @{ Label='Engine server.py'; Path=(Join-Path $EngineDir 'server.py'); Severity='WARN' },
    @{ Label='Engine DB'; Path=(Join-Path $EngineDir 'aura_quant_x.db'); Severity='INFO' },
    @{ Label='Knowledge Base'; Path=(Join-Path $EngineDir 'kb_weights.json'); Severity='INFO' },
    @{ Label='Model weights'; Path=(Join-Path $EngineDir 'model_weights.pt'); Severity='INFO' },
    @{ Label='Runtime events'; Path=(Join-Path $EngineDir 'runtime_events.jsonl'); Severity='INFO' },
    @{ Label='Bridge server'; Path=(Join-Path $BridgeDir 'server.py'); Severity='WARN' },
    @{ Label='Voice server'; Path=(Join-Path $BridgeDir 'jarvis_voice_server.py'); Severity='INFO' },
    @{ Label='Voice config'; Path=(Join-Path $JarvisDir 'config.yaml'); Severity='INFO' },
    @{ Label='Voice reference'; Path=(Join-Path $JarvisDir 'voices\reference.wav'); Severity='INFO' },
    @{ Label='Extension manifest'; Path=(Join-Path $ExtensionDir 'manifest.json'); Severity='INFO' },
    @{ Label='Extension background'; Path=(Join-Path $ExtensionDir 'background.js'); Severity='INFO' },
    @{ Label='Extension content'; Path=(Join-Path $ExtensionDir 'content.js'); Severity='INFO' },
    @{ Label='Train report'; Path=(Join-Path $EngineDir 'train_report.json'); Severity='INFO' },
    @{ Label='Model checksum'; Path=(Join-Path $EngineDir 'model_checksum.json'); Severity='INFO' },
    @{ Label='Bridge live feed'; Path=(Join-Path $BridgeDir 'live_feed.jsonl'); Severity='INFO' },
    @{ Label='Bridge latest'; Path=(Join-Path $BridgeDir 'live_latest.json'); Severity='INFO' },
    @{ Label='CornerAI entries log'; Path=(Join-Path $BridgeDir 'CornerAI_Log_Analises_Entradas.md'); Severity='INFO' },
    @{ Label='Voice README'; Path=(Join-Path $BridgeDir 'VOZ_LEIA_ME.md'); Severity='INFO' }
)

# Entradas esperadas que indicam o ecossistema descrito no manual.
$ExpectedPaths = @(
    'engine',
    'bridge',
    'extensao',
    'diagnostics',
    'MANUAL.md',
    'INSTALAR_E_INICIAR_TUDO.bat',
    'VALIDAR_AURA_QUANT_X.bat',
    'VALIDAR_AURA_TESTES.bat',
    'VALIDAR_AURA_TESTES.py',
    'DIAGNOSTICO_AURA.bat',
    'DIAGNOSTICO_AURA.ps1',
    'DIAGNOSTICO_WINDOWS_COMPLETO.bat',
    'DIAGNOSTICO_WINDOWS_COMPLETO.ps1',
    'RECUPERAR_AURA_SERVICOS.bat',
    'RECUPERAR_AURA_SERVICOS.ps1',
    'AUDITORIA_ERROS_ENGINE_OFF.md',
    'ORQUESTRADOR_AURA.md',
    'PERFIL_VOZ_AURA.md'
)

# ================================================================
# UTILITÁRIOS DE SAÍDA / FORMATAÇÃO
# ================================================================
function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Host ('=' * 68) -ForegroundColor DarkCyan
    Write-Host (' ' + $Title) -ForegroundColor Cyan
    Write-Host ('=' * 68) -ForegroundColor DarkCyan
}

function Write-StatusLine {
    param(
        [string]$Label,
        [string]$Value,
        [ValidateSet('OK','WARN','ERROR','INFO','MUTED')][string]$Level='INFO'
    )
    $color = switch ($Level) {
        'OK' { 'Green' }
        'WARN' { 'Yellow' }
        'ERROR' { 'Red' }
        'MUTED' { 'DarkGray' }
        default { 'White' }
    }
    Write-Host ('  {0,-31} {1}' -f $Label,$Value) -ForegroundColor $color
}

function Write-KVTable {
    param([hashtable]$Data)
    foreach ($key in $Data.Keys) {
        $value = [string]$Data[$key]
        if ($value.Length -gt 120) { $value = $value.Substring(0,117) + '...' }
        Write-Host ('  {0,-31} {1}' -f $key,$value) -ForegroundColor White
    }
}

function Get-SafeInt {
    param($Value, [int]$Default=0)
    try { return [int]$Value } catch { return $Default }
}

function Get-SafeDouble {
    param($Value, [double]$Default=0)
    try { return [double]$Value } catch { return $Default }
}

function ConvertTo-SafeJson {
    param($Object)
    try { return ($Object | ConvertTo-Json -Depth 10 -Compress) } catch { return '{}' }
}

function Truncate-Text {
    param([string]$Text, [int]$Max=180)
    if ($null -eq $Text) { return '' }
    $t = $Text.Trim()
    if ($t.Length -le $Max) { return $t }
    return $t.Substring(0,$Max-3) + '...'
}

# ================================================================
# PATH / ARQUIVO / DATA
# ================================================================
function Get-PathState {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return [pscustomobject]@{ Exists=$false; Type='NONE'; Size=0; LastWrite=$null; AgeSec=$null }
    }
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{ Exists=$false; Type='NONE'; Size=0; LastWrite=$null; AgeSec=$null }
    }
    try {
        $item = Get-Item -LiteralPath $Path -Force
        $last = $item.LastWriteTime
        $age = [math]::Round(((Get-Date) - $last).TotalSeconds,1)
        $size = if ($item.PSIsContainer) { 0 } else { $item.Length }
        $type = if ($item.PSIsContainer) { 'DIR' } else { 'FILE' }
        return [pscustomobject]@{ Exists=$true; Type=$type; Size=$size; LastWrite=$last; AgeSec=$age }
    } catch {
        return [pscustomobject]@{ Exists=$false; Type='ERROR'; Size=0; LastWrite=$null; AgeSec=$null }
    }
}

function Get-FileVersionText {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $lines = Get-Content -LiteralPath $Path -TotalCount 40 -ErrorAction Stop
        foreach ($line in $lines) {
            if ($line -match '(?i)(version\s*[:=]|vers[aã]o\s*[:=])') {
                return (Truncate-Text $line 160)
            }
        }
    } catch {}
    return $null
}

# ================================================================
# TCP / HTTP
# ================================================================
function Test-PortOk {
    param([Parameter(Mandatory=$true)][int]$Port)
    $client = $null
    $async = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect('127.0.0.1',$Port,$null,$null)
        if (-not $async.AsyncWaitHandle.WaitOne($TcpTimeoutMs,$false)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch { return $false }
    finally {
        if ($client) { try { $client.Close() } catch {} }
    }
}

function Invoke-LocalGet {
    param([Parameter(Mandatory=$true)][string]$Url, [int]$TimeoutSec=$HttpTimeoutSeconds)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $result = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec $TimeoutSec -Headers @{ 'Cache-Control'='no-cache' } -ErrorAction Stop
        $sw.Stop()
        return [pscustomobject]@{
            Ok=$true; Status=200; LatencyMs=[int]$sw.ElapsedMilliseconds; Body=$result; Error=$null; Url=$Url
        }
    } catch {
        $sw.Stop()
        $status = 0
        try { if ($_.Exception.Response) { $status=[int]$_.Exception.Response.StatusCode } } catch {}
        return [pscustomobject]@{
            Ok=$false; Status=$status; LatencyMs=[int]$sw.ElapsedMilliseconds; Body=$null; Error=(Truncate-Text $_.Exception.Message 240); Url=$Url
        }
    }
}

function Invoke-EndpointFallback {
    param([Parameter(Mandatory=$true)]$Service)
    $urls = @($Service.PrimaryUrl) + @($Service.FallbackUrls)
    foreach ($url in $urls) {
        $r = Invoke-LocalGet -Url $url
        if ($r.Ok) { return $r }
    }
    return $r
}

function Get-ServiceSnapshot {
    param([Parameter(Mandatory=$true)]$Service)
    $tcp = Test-PortOk -Port $Service.Port
    if (-not $tcp) {
        return [pscustomobject]@{
            Name=$Service.Name; Port=$Service.Port; Role=$Service.Role; Critical=$Service.Critical;
            Tcp=$false; Http=$false; Status=0; LatencyMs=0; Url=$Service.PrimaryUrl; Body=$null; Error='porta TCP indisponível';
            State='OFFLINE'
        }
    }
    $health = Invoke-EndpointFallback -Service $Service
    $state = if ($health.Ok) {
        if ($health.LatencyMs -gt $MaxHealthLatencyMs) { 'SLOW' } else { 'ONLINE' }
    } else { 'TCP_ONLY' }
    return [pscustomobject]@{
        Name=$Service.Name; Port=$Service.Port; Role=$Service.Role; Critical=$Service.Critical;
        Tcp=$true; Http=$health.Ok; Status=$health.Status; LatencyMs=$health.LatencyMs;
        Url=$health.Url; Body=$health.Body; Error=$health.Error; State=$state
    }
}

function Get-EndpointStatus {
    param([hashtable]$Endpoint)
    $r = Invoke-LocalGet -Url $Endpoint.Url
    return [pscustomobject]@{
        Name=$Endpoint.Name; Url=$Endpoint.Url; Ok=$r.Ok; Status=$r.Status; LatencyMs=$r.LatencyMs;
        Body=$r.Body; Error=$r.Error
    }
}

function Test-ExtensionCorsHealth {
    $url='http://127.0.0.1:8099/api/voice/health'
    $origin='chrome-extension://aura-monitor-audit'
    try {
        $r=Invoke-WebRequest -Uri $url -Method Get -TimeoutSec $HttpTimeoutSeconds -UseBasicParsing -Headers @{ Origin=$origin; 'Cache-Control'='no-cache' } -ErrorAction Stop
        $allow=$r.Headers['Access-Control-Allow-Origin']
        return [pscustomobject]@{ Ok=($r.StatusCode -ge 200 -and $r.StatusCode -lt 500); Status=[int]$r.StatusCode; Allowed=$allow; Error=$null }
    } catch {
        return [pscustomobject]@{ Ok=$false; Status=0; Allowed=$null; Error=(Truncate-Text $_.Exception.Message 220) }
    }
}

function Get-ManualAuditSnapshot {
    $manual=Join-Path $Root 'MANUAL.md'
    $out=[ordered]@{ Exists=$false; HeaderVersion=$null; FooterVersion=$null; UpdateDate=$null; Section8Reference=$false; Section8Exists=$false; InconsistentVersionText=$false; Notes=@() }
    if(-not(Test-Path -LiteralPath $manual)){ $out.Notes=@('MANUAL.md ausente'); return [pscustomobject]$out }
    $out.Exists=$true
    try {
        $lines=Get-Content -LiteralPath $manual -ErrorAction Stop
        $text=$lines -join "`n"
        if($text -match '(?i)Versão atual:\s*([^\r\n]+)'){$out.HeaderVersion=$Matches[1].Trim()}
        if($text -match '(?i)Fim do Manual\s+v([^\s—-]+)'){$out.FooterVersion=$Matches[1].Trim()}
        if($text -match '(?i)Última auditoria / atualização deste manual:\s*([^\r\n]+)'){$out.UpdateDate=$Matches[1].Trim()}
        $out.Section8Reference=($text -match '(?i)seção 8')
        $out.Section8Exists=($text -match '(?m)^8[.)\s]')
        $out.InconsistentVersionText=(([string]$out.HeaderVersion -notlike '12.6.17*') -or ([string]$out.FooterVersion -and [string]$out.FooterVersion -ne '12.6.17'))
        if($out.Section8Reference -and -not $out.Section8Exists){$out.Notes += 'Manual referencia seção 8, mas a seção 8 não aparece como seção formal.'}
        if($out.FooterVersion -eq '12.5.10' -and $out.HeaderVersion -like '12.6.17*'){$out.Notes += 'Rodapé antigo 12.5.10 permanece sob cabeçalho 12.6.17.'}
        try { if($out.UpdateDate -and ([datetime]$out.UpdateDate) -lt (Get-Date).Date){$out.Notes += 'Última atualização do manual é anterior à data atual; revisar antes do release.'} } catch {}
    } catch { $out.Notes += 'Falha de leitura do MANUAL.md: '+(Truncate-Text $_.Exception.Message 180) }
    return [pscustomobject]$out
}

function Get-RouteCoverageAudit {
    $routes=Get-DocumentedRouteRegistry
    $read=@($routes | Where-Object {-not $_.Mutating})
    $tested=@($EngineEndpoints + $BridgeEndpoints + $VoiceEndpoints | ForEach-Object {$_.Name})
    $uncovered=@()
    foreach($r in $read){
        $short=$r.Route
        if(-not (@($tested | Where-Object { $_ -like ('*'+$short+'*') }).Count -gt 0)){$uncovered += $r.Route}
    }
    return [pscustomobject]@{Documented=$routes.Count;ReadOnly=$read.Count;ReadOnlyUncovered=@($uncovered)}
}

function Get-SqliteSchemaSnapshot {
    $py=Join-Path $EngineDir 'venv\Scripts\python.exe'
    $db=Join-Path $EngineDir 'aura_quant_x.db'
    if(-not(Test-Path -LiteralPath $db)){return [pscustomobject]@{Available=$false;Tables=@();Error='SQLite inexistente'}}
    if(-not(Test-Path -LiteralPath $py)){return [pscustomobject]@{Available=$false;Tables=@();Error='Python venv inexistente'}}
    try {
        $code=@'
import sqlite3, json, sys
p=sys.argv[1]
c=sqlite3.connect(p)
rows=c.execute("select name from sqlite_master where type='table' order by name").fetchall()
print(json.dumps([r[0] for r in rows], ensure_ascii=False))
'@
        $txt=& $py -c $code $db 2>$null | Out-String
        $tables=@($txt.Trim() | ConvertFrom-Json -ErrorAction Stop)
        return [pscustomobject]@{Available=$true;Tables=@($tables);Error=$null}
    } catch {return [pscustomobject]@{Available=$false;Tables=@();Error=(Truncate-Text $_.Exception.Message 200)}}
}

function Get-WomSnapshot {
    param($Analysis)
    $m=$null; if($null -ne $Analysis -and $Analysis.Available){$m=$Analysis.Market}
    if($null -eq $m){return [pscustomobject]@{Available=$false;Velocity=$null;Confluence=$null;Blocked=$null;Line=$null;Odds=$null;Interpretation='sem dados WoM expostos'}}
    $v=$null;$c=$null;$b=$null;$line=$null;$odds=$null
    foreach($n in @('odds_velocity','velocity')){if($null -eq $v -and $m.PSObject.Properties.Name -contains $n){$v=$m.$n}}
    foreach($n in @('confluence')){if($null -eq $c -and $m.PSObject.Properties.Name -contains $n){$c=$m.$n}}
    foreach($n in @('blocked','wom_blocked')){if($null -eq $b -and $m.PSObject.Properties.Name -contains $n){$b=$m.$n}}
    foreach($n in @('asian_corner_line','line')){if($null -eq $line -and $m.PSObject.Properties.Name -contains $n){$line=$m.$n}}
    foreach($n in @('asian_corner_odds','odds')){if($null -eq $odds -and $m.PSObject.Properties.Name -contains $n){$odds=$m.$n}}
    $interp='neutro/indisponível'
    try { if([double]$v -gt 1.5){$interp='divergência > 1,5% — bloquear BUY_CORNER'} elseif([double]$v -le -5){$interp='confluência <= -5%'} }catch{}
    return [pscustomobject]@{Available=$true;Velocity=$v;Confluence=$c;Blocked=$b;Line=$line;Odds=$odds;Interpretation=$interp;Raw=$m}
}

# ================================================================
# SISTEMA WINDOWS / HARDWARE
# ================================================================
function Get-SystemSnapshot {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'" -ErrorAction SilentlyContinue
    $cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
    $gpu = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue

    $totalGB = $null
    $freeGB = $null
    $usedGB = $null
    $memFreePct = $null
    if ($cs -and $os) {
        $totalGB = [math]::Round($cs.TotalPhysicalMemory / 1GB,1)
        $freeGB = [math]::Round(($os.FreePhysicalMemory * 1KB) / 1GB,1)
        $usedGB = [math]::Round($totalGB-$freeGB,1)
        if ($totalGB -gt 0) { $memFreePct=[math]::Round(($freeGB/$totalGB)*100,1) }
    }

    $osText=$null;$osVersion=$null;$cpuText=$null;$cpuCores=$null;$diskFree=$null;$diskTotal=$null
    if($os){$osText=$os.Caption;$osVersion=$os.Version}
    if($cpu){$cpuText=$cpu.Name;$cpuCores=$cpu.NumberOfLogicalProcessors}
    if($disk){$diskFree=[math]::Round($disk.FreeSpace/1GB,1);$diskTotal=[math]::Round($disk.Size/1GB,1)}
    return [pscustomobject]@{
        ComputerName=$env:COMPUTERNAME
        OS=$osText
        OSVersion=$osVersion
        PowerShell=$PSVersionTable.PSVersion.ToString()
        CPU=$cpuText
        CPUCores=$cpuCores
        RAMTotalGB=$totalGB; RAMUsedGB=$usedGB; RAMFreeGB=$freeGB; RAMFreePct=$memFreePct
        DiskFreeGB=$diskFree
        DiskTotalGB=$diskTotal
        GPUCount=@($gpu).Count
        GPUs=@($gpu | ForEach-Object { $_.Name })
    }
}

function Get-GpuRuntimeSnapshot {
    $result = [ordered]@{ nvidiaSmi=$false; nvidiaSmiText=$null; TorchCuda=$false; TorchVersion=$null; Python=$null; Pythons=@() }

    $pythonCandidates = @(
        (Join-Path $EngineDir 'venv\Scripts\python.exe'),
        (Join-Path $Root 'venv\Scripts\python.exe'),
        'python.exe'
    )
    foreach ($p in $pythonCandidates) {
        try {
            $cmd = Get-Command $p -ErrorAction SilentlyContinue
            if ($cmd) { $result.Pythons += $cmd.Source }
        } catch {}
    }
    $result.Pythons = @($result.Pythons | Sort-Object -Unique)
    if ($result.Pythons.Count -gt 0) { $result.Python=$result.Pythons[0] }

    try {
        $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
        if ($smi) {
            $result.nvidiaSmi=$true
            $result.nvidiaSmiText=(nvidia-smi --query-gpu=name,memory.total,memory.used,driver_version --format=csv,noheader 2>$null | Out-String).Trim()
        }
    } catch {}

    if ($result.Python) {
        try {
            $py = & $result.Python -c "import sys; print(sys.version);\ntry:\n import torch; print('TORCH_VERSION='+str(torch.__version__)); print('TORCH_CUDA='+str(bool(torch.cuda.is_available())))\nexcept Exception as e: print('TORCH_ERROR='+str(e))" 2>$null | Out-String
            if ($py -match 'TORCH_VERSION=([^\r\n]+)') { $result.TorchVersion=$Matches[1].Trim() }
            if ($py -match 'TORCH_CUDA=True') { $result.TorchCuda=$true }
        } catch {}
    }
    return [pscustomobject]$result
}

function Get-PowerShellEnvironmentSnapshot {
    $vars = @('CORNERAI_TG_TOKEN','CORNERAI_TG_CHAT','CORNERAI_BRIDGE_TOKEN','OLLAMA_HOST')
    $out = [ordered]@{}
    foreach ($v in $vars) {
        $present = -not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($v))
        # Nunca imprime segredo, somente existência.
        $envState='não definido'; if($present){$envState='CONFIGURADO'}
        $out[$v] = $envState
    }
    return [pscustomobject]$out
}

# ================================================================
# PROCESSOS
# ================================================================
function Get-ProcessSnapshot {
    $names = 'python','python3','ollama','ollama_llama_server','node','electron','aura','chrome'
    $rows = @()
    foreach ($p in (Get-Process -ErrorAction SilentlyContinue)) {
        if ($p.ProcessName -match '(?i)^(python|python3|ollama|ollama_llama_server|node|electron|aura|chrome)$') {
            $cpu = 0
            try { $cpu=[math]::Round($p.CPU,1) } catch {}
            $ram=[math]::Round($p.WorkingSet64/1MB,1)
            $startTime=$null
            try { $startTime=$p.StartTime } catch {}
            $rows += [pscustomobject]@{ Id=$p.Id; Name=$p.ProcessName; RAM_MB=$ram; CPU_s=$cpu; StartTime=$startTime }
        }
    }
    return @($rows | Sort-Object RAM_MB -Descending)
}

function Get-PortOwners {
    param([int[]]$Ports=(11434,8080,8765,8099))
    $rows=@()
    try {
        $net = Get-NetTCPConnection -State Listen -ErrorAction Stop
        foreach ($n in $net) {
            if ($Ports -contains $n.LocalPort) {
                $proc=$null
                try { $proc=Get-Process -Id $n.OwningProcess -ErrorAction SilentlyContinue } catch {}
                $procName='?'; if($proc){$procName=$proc.ProcessName}
                $rows += [pscustomobject]@{ Port=$n.LocalPort; PID=$n.OwningProcess; Process=$procName; LocalAddress=$n.LocalAddress }
            }
        }
    } catch {
        foreach ($p in $Ports) {
            $rows += [pscustomobject]@{ Port=$p; PID='n/a'; Process='Get-NetTCPConnection indisponível'; LocalAddress='n/a' }
        }
    }
    return @($rows | Sort-Object Port,PID)
}

# ================================================================
# OLLAMA / MODELO
# ================================================================
function Get-OllamaSnapshot {
    $tags = Invoke-LocalGet -Url 'http://127.0.0.1:11434/api/tags'
    $models=@()
    if ($tags.Ok -and $tags.Body) {
        try {
            foreach ($m in @($tags.Body.models)) {
                $modelSize=$null; if($m.size){$modelSize=[math]::Round(([double]$m.size)/1GB,2)}
                $models += [pscustomobject]@{
                    Name=$m.name
                    SizeGB=$modelSize
                    Modified=$m.modified_at
                }
            }
        } catch {}
    }
    $preferred = @('llama3.2:3b','llama3.1:8b','llama3.1:8b-instruct-q8_0','qwen2.5:14b-instruct-q4_0','qwen2.5:32b-instruct-q4_0','llama3.1:70b-instruct-q4_0')
    $presentNames=@($models | ForEach-Object {$_.Name})
    $fallbacks=@($preferred | Where-Object { $presentNames -contains $_ })
    return [pscustomobject]@{
        Online=$tags.Ok
        LatencyMs=$tags.LatencyMs
        Models=@($models)
        AvailableNames=$presentNames
        PreferredInstalled=$fallbacks
        HasRecommended=$fallbacks.Count -gt 0
        Error=$tags.Error
    }
}

# ================================================================
# MANIFEST / EXTENSÃO / PYTHON / VENV
# ================================================================
function Get-ManifestSnapshot {
    $path=Join-Path $ExtensionDir 'manifest.json'
    if (-not (Test-Path -LiteralPath $path)) { return [pscustomobject]@{ Exists=$false; Valid=$false; Version=$null; MV3=$false; SidePanel=$false; HostPermissions=@(); ContentScripts=@(); Error='manifest ausente' } }
    try {
        $raw=Get-Content -LiteralPath $path -Raw -ErrorAction Stop
        $m=$raw | ConvertFrom-Json -ErrorAction Stop
        return [pscustomobject]@{
            Exists=$true; Valid=$true; Version=$m.version; MV3=([string]$m.manifest_version -eq '3');
            SidePanel=($null -ne $m.side_panel); HostPermissions=@($m.host_permissions); ContentScripts=@($m.content_scripts); Error=$null
        }
    } catch {
        return [pscustomobject]@{ Exists=$true; Valid=$false; Version=$null; MV3=$false; SidePanel=$false; HostPermissions=@(); ContentScripts=@(); Error=(Truncate-Text $_.Exception.Message 200) }
    }
}

function Get-VenvSnapshot {
    $venv=Join-Path $EngineDir 'venv'
    $python=Join-Path $venv 'Scripts\python.exe'
    $pip=Join-Path $venv 'Scripts\pip.exe'
    $imports=@()
    if (Test-Path -LiteralPath $python) {
        $checks=@('numpy','torch','yaml')
        foreach($mod in $checks){
            try {
                & $python -c "import $mod; print('OK')" 2>$null | Out-Null
                if($LASTEXITCODE -eq 0){$imports += "$mod=OK"}else{$imports += "$mod=FAIL"}
            } catch { $imports += "$mod=FAIL" }
        }
    }
    return [pscustomobject]@{
        Exists=(Test-Path -LiteralPath $venv)
        PythonExists=(Test-Path -LiteralPath $python)
        PipExists=(Test-Path -LiteralPath $pip)
        Python=$python
        Imports=$imports
    }
}

# ================================================================
# PERSISTÊNCIA / RUNTIME / FEED
# ================================================================
function Get-PersistenceSnapshot {
    $files=@()
    foreach($entry in $CriticalFiles){
        $st=Get-PathState -Path $entry.Path
        $files += [pscustomobject]@{
            Label=$entry.Label; Path=$entry.Path; Exists=$st.Exists; Type=$st.Type; SizeKB=[math]::Round($st.Size/1KB,1); LastWrite=$st.LastWrite; AgeSec=$st.AgeSec; Severity=$entry.Severity
        }
    }

    $runtime=@()
    if(Test-Path -LiteralPath $RuntimeDir){
        $runtime=Get-ChildItem -LiteralPath $RuntimeDir -File -Recurse -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 20 FullName,LastWriteTime,@{N='SizeKB';E={[math]::Round($_.Length/1KB,1)}}
    }

    $latest=@()
    $feedCandidates=@(
        (Join-Path $BridgeDir 'live_feed.jsonl'),
        (Join-Path $BridgeDir 'live_latest.json'),
        (Join-Path $BridgeDir 'CornerAI_Log_Analises_Entradas.md')
    )
    foreach($f in $feedCandidates){
        if(Test-Path -LiteralPath $f){
            $st=Get-PathState -Path $f
            $latest += [pscustomobject]@{ Path=$f; LastWrite=$st.LastWrite; AgeSec=$st.AgeSec; SizeKB=[math]::Round($st.Size/1KB,1) }
        }
    }
    return [pscustomobject]@{ Files=$files; Runtime=$runtime; Feeds=$latest }
}

function Get-JsonlTail {
    param([string]$Path,[int]$Lines=20)
    if(-not(Test-Path -LiteralPath $Path)){ return @() }
    try { return @(Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction Stop) } catch { return @() }
}

function Get-LiveFeedSnapshot {
    $files=@(
        (Join-Path $BridgeDir 'live_feed.jsonl'),
        (Join-Path $BridgeDir 'live_latest.json')
    )
    $out=@()
    foreach($f in $files){
        $st=Get-PathState -Path $f
        if($st.Exists){
            $out += [pscustomobject]@{File=$f; AgeSec=$st.AgeSec; LastWrite=$st.LastWrite; SizeKB=[math]::Round($st.Size/1KB,1); Fresh=($st.AgeSec -le $FeedStaleSeconds)}
        }
    }
    return @($out)
}

# ================================================================
# LOGS / ERROS / OBSERVABILIDADE
# ================================================================
function Get-LogFiles {
    $files=@()
    foreach($p in $OfficialLogs){ if(Test-Path -LiteralPath $p){$files += $p} }
    foreach($base in @($RuntimeDir,$EngineDir,$BridgeDir,$DiagnosticsDir)){
        if(Test-Path -LiteralPath $base){
            $files += Get-ChildItem -LiteralPath $base -File -Recurse -ErrorAction SilentlyContinue |
                Where-Object { $_.Extension -in '.log','.jsonl','.txt' } |
                Select-Object -ExpandProperty FullName
        }
    }
    return @($files | Sort-Object -Unique)
}

function Get-RecentLogErrors {
    $patterns='ERROR|Traceback|Exception|CRITICAL|failed|failure|falha|offline|timeout|refused|BLOCK|blocked|invalid|reconcil|diverg|cooldown|no_edge|prob_below|smart_money_divergence'
    $rows=@()
    foreach($file in (Get-LogFiles)){
        try {
            $lines=Get-Content -LiteralPath $file -Tail $MaxLogLinesPerFile -ErrorAction Stop
            $ln=0
            foreach($line in $lines){
                $ln++
                if($line -match $patterns){
                    $rows += [pscustomobject]@{ File=(Split-Path $file -Leaf); LineNumber=$ln; Line=(Truncate-Text $line 240); Path=$file }
                }
            }
        } catch {}
    }
    return @($rows | Select-Object -Last $MaxErrorsShown)
}

function Get-LastStructuredEvent {
    $path=Join-Path $EngineDir 'runtime_events.jsonl'
    $lines=Get-JsonlTail -Path $path -Lines 3
    foreach($line in ($lines | Sort-Object -Descending)){
        try {
            $obj=$line | ConvertFrom-Json -ErrorAction Stop
            return [pscustomobject]@{
                Ok=$true; Event=$obj; Raw=(Truncate-Text $line 320)
            }
        } catch {}
    }
    return [pscustomobject]@{Ok=$false;Event=$null;Raw=$null}
}

function Get-AuditFileSnapshot {
    $path=$SelfAuditLog
    if(-not(Test-Path -LiteralPath $path)){ return [pscustomobject]@{Exists=$false;Entries=0;Last=$null} }
    $lines=Get-JsonlTail -Path $path -Lines 1
    $lastLine=$null; if($lines.Count -gt 0){$lastLine=Truncate-Text $lines[0] 280}
    return [pscustomobject]@{Exists=$true;Entries=@(Get-Content -LiteralPath $path -ErrorAction SilentlyContinue).Count;Last=$lastLine}
}

function Write-SelfAudit {
    param([hashtable]$Snapshot)
    if(-not $EnableSelfAuditLog){return}
    try {
        $dir=Split-Path $SelfAuditLog -Parent
        if(-not(Test-Path -LiteralPath $dir)){New-Item -ItemType Directory -Path $dir -Force | Out-Null}
        $record=[ordered]@{
            timestamp=(Get-Date).ToString('o')
            monitor_version=$ScriptVersion
            services=$Snapshot.Services
            global_state=$Snapshot.GlobalState
            critical_errors=$Snapshot.CriticalErrors
        }
        Add-Content -LiteralPath $SelfAuditLog -Value (ConvertTo-SafeJson $record) -Encoding UTF8
    } catch {}
}

# ================================================================
# INTEGRIDADE DO FEED / DADOS
# ================================================================
function Get-LatestFeedRecord {
    $feed=Join-Path $BridgeDir 'live_feed.jsonl'
    $lines=Get-JsonlTail -Path $feed -Lines 3
    foreach($line in ($lines | Select-Object -Reverse)){
        try { return ($line | ConvertFrom-Json -ErrorAction Stop) } catch {}
    }
    return $null
}

function Get-DataIntegritySnapshot {
    $obj=Get-LatestFeedRecord
    if($null -eq $obj){
        return [pscustomobject]@{ HasData=$false; Status='NO_RECENT_RECORD'; FixtureId=$null; Minute=$null; Home=$null; Away=$null; CornersStat=$null; CornersEvent=$null; Reconciled=$null; Fresh=$false; NegativeCount=0; Conflict=$false; Note='Nenhum registro JSONL válido recente.' }
    }
    $fixture=$null; $minute=$null; $home=$null; $away=$null
    $cornStat=$null; $cornEvent=$null; $ts=$null
    foreach($name in @('fixtureId','fixture_id','id')){ if($null -eq $fixture -and $obj.PSObject.Properties.Name -contains $name){$fixture=$obj.$name} }
    foreach($name in @('minute','clock','match_minute')){ if($null -eq $minute -and $obj.PSObject.Properties.Name -contains $name){$minute=$obj.$name} }
    foreach($name in @('homeTeam','home','home_team')){ if($null -eq $home -and $obj.PSObject.Properties.Name -contains $name){$home=$obj.$name} }
    foreach($name in @('awayTeam','away','away_team')){ if($null -eq $away -and $obj.PSObject.Properties.Name -contains $name){$away=$obj.$name} }
    foreach($name in @('corners','corner_stats','corners_statistical','statistics_corners')){ if($null -eq $cornStat -and $obj.PSObject.Properties.Name -contains $name){$cornStat=$obj.$name} }
    foreach($name in @('corner_events','corners_event','event_corners','events_corners')){ if($null -eq $cornEvent -and $obj.PSObject.Properties.Name -contains $name){$cornEvent=$obj.$name} }
    foreach($name in @('timestamp','ts','captured_at','updated_at')){ if($null -eq $ts -and $obj.PSObject.Properties.Name -contains $name){$ts=$obj.$name} }

    $negative=0
    foreach($property in @('home_corners','away_corners','xg_home','xg_away','dangerous_home','dangerous_away','pressure_home','pressure_away')){
        if($obj.PSObject.Properties.Name -contains $property){ try{ if([double]$obj.$property -lt 0){$negative++} }catch{} }
    }

    $reconciled=$null
    try {
        if($null -ne $cornStat -and $null -ne $cornEvent){
            if($cornStat -is [System.Management.Automation.PSCustomObject] -and $cornEvent -is [System.Management.Automation.PSCustomObject]){
                $reconciled=$null
            } elseif([double]$cornStat -eq [double]$cornEvent){ $reconciled=$true } else { $reconciled=$false }
        }
    } catch {}

    $fresh=$true
    if($ts){
        try {
            $parsed=[datetime]$ts
            $fresh=((Get-Date)-$parsed).TotalSeconds -le $FeedStaleSeconds
        } catch {}
    } else {
        $feedState=Get-PathState -Path (Join-Path $BridgeDir 'live_feed.jsonl')
        if($feedState.AgeSec -ne $null){$fresh=$feedState.AgeSec -le $FeedStaleSeconds}
    }

    $status='OK'
    if($negative -gt 0 -or $reconciled -eq $false){$status='BLOCK'}
    elseif(-not $fresh){$status='STALE'}
    $noteText='Sem bloqueador estrutural detectado.'
    if($status -eq 'BLOCK'){$noteText='Integridade bloqueada: reconciliação/valores inválidos.'}
    elseif($status -eq 'STALE'){$noteText='Feed atrasado acima do limite documentado.'}

    return [pscustomobject]@{
        HasData=$true; Status=$status; FixtureId=$fixture; Minute=$minute; Home=$home; Away=$away;
        CornersStat=$cornStat; CornersEvent=$cornEvent; Reconciled=$reconciled; Fresh=$fresh;
        NegativeCount=$negative; Conflict=($reconciled -eq $false);
        Note=$noteText

    }
}

# ================================================================
# ANÁLISE / RISK / ORQUESTRADOR
# ================================================================
function Get-OrchestratorSnapshot {
    $r=Invoke-LocalGet -Url 'http://127.0.0.1:8765/api/orchestrator/status'
    return [pscustomobject]@{Ok=$r.Ok;Status=$r.Status;LatencyMs=$r.LatencyMs;Body=$r.Body;Error=$r.Error}
}

function Get-AnalysisSnapshot {
    $feed=Get-LatestFeedRecord
    if($null -eq $feed){ return [pscustomobject]@{Available=$false;FixtureId=$null;Decision=$null;Reason=$null;RiskGate=$null;Edge=$null;Uncertainty=$null;DataIntegrity=$null;Market=$null;Raw=$null} }
    $fx=$null
    foreach($n in @('fixtureId','fixture_id','id')){if($null -eq $fx -and $feed.PSObject.Properties.Name -contains $n){$fx=$feed.$n}}
    if([string]::IsNullOrWhiteSpace([string]$fx)){return [pscustomobject]@{Available=$false;FixtureId=$null;Decision=$null;Reason=$null;RiskGate=$null;Edge=$null;Uncertainty=$null;DataIntegrity=$null;Market=$null;Raw=$null}}
    $url='http://127.0.0.1:8765/api/analysis/' + [uri]::EscapeDataString([string]$fx)
    $r=Invoke-LocalGet -Url $url
    if(-not $r.Ok){return [pscustomobject]@{Available=$false;FixtureId=$fx;Decision=$null;Reason=$null;RiskGate=$null;Edge=$null;Uncertainty=$null;DataIntegrity=$null;Market=$null;Raw=$null;Error=$r.Error}}
    $b=$r.Body
    $decisionValue=$null
    if($b.decision){$decisionValue=$b.decision}elseif($b.signal){$decisionValue=$b.signal}
    return [pscustomobject]@{
        Available=$true;FixtureId=$fx;
        Decision=$decisionValue;
        Reason=$b.reason;RiskGate=$b.risk_gate;Edge=$b.edge;Uncertainty=$b.uncertainty;
        DataIntegrity=$b.data_integrity;Market=$b.market;Raw=$b
    }
}

function Get-RiskPresentation {
    param($Analysis)
    if(-not $Analysis.Available){ return [pscustomobject]@{State='UNAVAILABLE';Reason='Análise não disponível.';Gate=$null} }
    $decision=[string]$Analysis.Decision
    $gate=[string]$Analysis.RiskGate
    $reason=[string]$Analysis.Reason
    if($decision -match 'BLOCK' -or $gate -match 'BLOCK|DENY|FAIL'){ $state='BLOCK' }
    elseif($decision -match 'BUY'){ $state='BUY_PRESENT_BUT_VALIDATE'
    } elseif($decision -match 'WATCH'){ $state='WATCH_ONLY' }
    elseif($decision -match 'HOLD'){ $state='HOLD' }
    else { $state='OBSERVE' }
    return [pscustomobject]@{State=$state;Reason=$reason;Gate=$gate;Decision=$decision}
}

# ================================================================
# WINDOWS DIAGNOSTICS READ-ONLY
# ================================================================
function Get-WindowsDiagnosticsSnapshot {
    $out=[ordered]@{
        FirewallCmd=$false
        DefenderCmd=$false
        DISM=$false
        SFC=$false
        DefenderRealtime=$null
        FirewallProfiles=$null
        Health=$(Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue)
    }
    try { if(Get-Command Get-NetFirewallProfile -ErrorAction SilentlyContinue){$out.FirewallCmd=$true;$out.FirewallProfiles=@(Get-NetFirewallProfile -ErrorAction SilentlyContinue | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction)} }catch{}
    try { if(Get-Command Get-MpComputerStatus -ErrorAction SilentlyContinue){$out.DefenderCmd=$true;$d=Get-MpComputerStatus -ErrorAction SilentlyContinue;$out.DefenderRealtime=$d.RealTimeProtectionEnabled} }catch{}
    try { if(Get-Command dism.exe -ErrorAction SilentlyContinue){$out.DISM=$true} }catch{}
    try { if(Get-Command sfc.exe -ErrorAction SilentlyContinue){$out.SFC=$true} }catch{}
    return [pscustomobject]$out
}

function Get-ExistingDiagnosticReports {
    if(-not(Test-Path -LiteralPath $DiagnosticsDir)){return @()}
    Get-ChildItem -LiteralPath $DiagnosticsDir -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 15 Name,LastWriteTime,@{N='SizeKB';E={[math]::Round($_.Length/1KB,1)}}
}

# ================================================================
# CLASSIFICAÇÃO GLOBAL
# ================================================================
function Get-GlobalState {
    param(
        [array]$Services,
        $DataIntegrity,
        $Venv,
        $Manifest,
        $Ollama,
        $Feeds,
        $Analysis
    )
    $critical=@($Services | Where-Object {$_.Critical -and $_.State -eq 'OFFLINE'})
    if($critical.Count -gt 0){return [pscustomobject]@{Level='CRITICAL';Reason=(($critical | ForEach-Object {$_.Name}) -join ', ') + ' offline'}}
    if($null -ne $DataIntegrity -and $DataIntegrity.Status -eq 'BLOCK'){return [pscustomobject]@{Level='BLOCKED';Reason=$DataIntegrity.Note}}
    if($null -ne $Venv -and -not $Venv.PythonExists){return [pscustomobject]@{Level='DEGRADED';Reason='Engine venv/python não encontrado'}}
    if($null -ne $Manifest -and $Manifest.Exists -and (-not $Manifest.Valid -or -not $Manifest.MV3)){return [pscustomobject]@{Level='DEGRADED';Reason='Manifest da extensão inválido ou não-MV3'}}
    if($null -ne $Ollama -and $Ollama.Online -and -not $Ollama.HasRecommended){return [pscustomobject]@{Level='DEGRADED';Reason='Ollama online, mas nenhum modelo recomendado documentado está instalado'}}
    foreach($s in $Services){if($s.State -in @('TCP_ONLY','SLOW')){return [pscustomobject]@{Level='DEGRADED';Reason=$s.Name + ' degradado'}}}
    foreach($f in $Feeds){if($f.Fresh -eq $false){return [pscustomobject]@{Level='DEGRADED';Reason='feed/runtime atrasado'}}}
    if($null -ne $Analysis -and $Analysis.Available){return [pscustomobject]@{Level='OPERATIONAL';Reason='Serviços e dados estruturais presentes; respeitar os gates do Engine'}}
    return [pscustomobject]@{Level='PARTIAL';Reason='Infraestrutura parcial; análise recente indisponível'}
}


# ================================================================
# INVENTÁRIO DA EXTENSÃO / MÓDULOS / STARTUP
# ================================================================
function Get-ComponentInventorySnapshot {
    $expected = @(
        'manifest.json','background.js','content.js','page-hook.js','market-capture.js',
        'menu-capture.js','h2h-capture.js','charts-unified.js','hud-overlay.js',
        'activation-diagnostic.js','local-ai-client.js','popup.html','popup.js',
        'ui\dashboard.js','ui\dashboard.css','ui\dashboard.html',
        'visao\sidepanel.html','visao\sidepanel.js','visao\voice-assistant.js','visao\chat.css',
        'lib\fixture-id.js','lib\clock-parse.js','lib\pressure-dual.js','lib\event-clock.js',
        'lib\ws-decode.js','lib\chunk-buffer.js','gemini-connector.js','gemini-live-connector.js'
    )
    $rows=@()
    foreach($rel in $expected){
        $path=Join-Path $ExtensionDir $rel
        $exists=Test-Path -LiteralPath $path
        $st=Get-PathState -Path $path
        $rows += [pscustomobject]@{ Component=$rel; Exists=$exists; SizeKB=[math]::Round($st.Size/1KB,1); LastWrite=$st.LastWrite }
    }
    return @($rows)
}

function Get-EngineModuleInventorySnapshot {
    $expected = @(
        'server.py','engine_core.py','risk_manager.py','data_store.py','features.py','metrics.py',
        'backtest_engine.py','train_pipeline.py','signals_service.py',
        'modules\auto_backtest_on_close.py','modules\auto_calibrate_risk.py','modules\paper_force_mode.py',
        'improvements\rate_limit.py','improvements\frame_cache.py','improvements\trading_mode.py',
        'improvements\notify_policy.py','improvements\health_agg.py','improvements\telemetry_schema.py','improvements\model_checksum.py',
        'reliability\watchdog.py','reliability\anomaly.py','reliability\cascade_guard.py','reliability\latency_forecast.py',
        'unified_pipeline.py','server_multi_agents_elite.py','server_multi_agents_v11.py','server_gpu_master.py',
        'server_elite_gpu.py','server_tier1_quant.py','aura_auto_evolver.py','aura_context_crawler.py'
    )
    $rows=@()
    foreach($rel in $expected){
        $path=Join-Path $EngineDir $rel
        $st=Get-PathState -Path $path
        $rows += [pscustomobject]@{ Module=$rel; Exists=$st.Exists; SizeKB=[math]::Round($st.Size/1KB,1); LastWrite=$st.LastWrite }
    }
    return @($rows)
}

function Get-VoiceInventorySnapshot {
    $expected=@(
        'jarvis_voice_server.py','jarvis\modules\stt.py','jarvis\modules\llm.py','jarvis\modules\tts.py',
        'jarvis\modules\device.py','jarvis\config.yaml','jarvis\voices\reference.wav',
        'iniciar_voz.bat','iniciar_voz.sh','VOZ_LEIA_ME.md'
    )
    $rows=@()
    foreach($rel in $expected){
        $path=Join-Path $BridgeDir $rel
        $st=Get-PathState -Path $path
        $rows += [pscustomobject]@{ File=$rel; Exists=$st.Exists; SizeKB=[math]::Round($st.Size/1KB,1); AgeSec=$st.AgeSec }
    }
    return @($rows)
}

function Get-StartupRecoverySnapshot {
    $expected=@(
        @{Name='INSTALAR_E_INICIAR_TUDO.bat';Path=(Join-Path $Root 'INSTALAR_E_INICIAR_TUDO.bat')},
        @{Name='VALIDAR_AURA_QUANT_X.bat';Path=(Join-Path $Root 'VALIDAR_AURA_QUANT_X.bat')},
        @{Name='VALIDAR_AURA_TESTES.bat';Path=(Join-Path $Root 'VALIDAR_AURA_TESTES.bat')},
        @{Name='VALIDAR_AURA_TESTES.py';Path=(Join-Path $Root 'VALIDAR_AURA_TESTES.py')},
        @{Name='DIAGNOSTICO_AURA.bat';Path=(Join-Path $Root 'DIAGNOSTICO_AURA.bat')},
        @{Name='DIAGNOSTICO_AURA.ps1';Path=(Join-Path $Root 'DIAGNOSTICO_AURA.ps1')},
        @{Name='DIAGNOSTICO_WINDOWS_COMPLETO.bat';Path=(Join-Path $Root 'DIAGNOSTICO_WINDOWS_COMPLETO.bat')},
        @{Name='DIAGNOSTICO_WINDOWS_COMPLETO.ps1';Path=(Join-Path $Root 'DIAGNOSTICO_WINDOWS_COMPLETO.ps1')},
        @{Name='RECUPERAR_AURA_SERVICOS.bat';Path=(Join-Path $Root 'RECUPERAR_AURA_SERVICOS.bat')},
        @{Name='RECUPERAR_AURA_SERVICOS.ps1';Path=(Join-Path $Root 'RECUPERAR_AURA_SERVICOS.ps1')}
    )
    $rows=@()
    foreach($e in $expected){
        $st=Get-PathState -Path $e.Path
        $rows += [pscustomobject]@{Name=$e.Name;Exists=$st.Exists;SizeKB=[math]::Round($st.Size/1KB,1);LastWrite=$st.LastWrite}
    }
    return @($rows)
}

function Get-DocumentedRouteRegistry {
    return @(
        [pscustomobject]@{Service='Bridge';Method='GET';Route='/health';Mutating=$false;Purpose='health'},
        [pscustomobject]@{Service='Bridge';Method='POST';Route='/api/cornerai/feed';Mutating=$true;Purpose='receber feed'},
        [pscustomobject]@{Service='Engine';Method='GET';Route='/health';Mutating=$false;Purpose='health'},
        [pscustomobject]@{Service='Engine';Method='GET';Route='/api/status';Mutating=$false;Purpose='status'},
        [pscustomobject]@{Service='Engine';Method='POST';Route='/api/telemetry';Mutating=$true;Purpose='telemetria'},
        [pscustomobject]@{Service='Engine';Method='POST';Route='/api/historical';Mutating=$true;Purpose='historical/treino'},
        [pscustomobject]@{Service='Engine';Method='GET';Route='/api/analysis/<fixtureId>';Mutating=$false;Purpose='análise'},
        [pscustomobject]@{Service='Engine';Method='POST';Route='/api/feedback';Mutating=$true;Purpose='feedback/Kb'},
        [pscustomobject]@{Service='Engine';Method='POST';Route='/api/telegram/test';Mutating=$true;Purpose='teste Telegram'},
        [pscustomobject]@{Service='Engine';Method='POST';Route='/api/telegram/alert';Mutating=$true;Purpose='alerta Telegram'},
        [pscustomobject]@{Service='Engine';Method='GET';Route='/api/orchestrator/status';Mutating=$false;Purpose='snapshot orquestrador'},
        [pscustomobject]@{Service='Engine';Method='GET';Route='/api/observability/events';Mutating=$false;Purpose='eventos estruturados'},
        [pscustomobject]@{Service='Voice';Method='GET';Route='/api/voice/health';Mutating=$false;Purpose='health'},
        [pscustomobject]@{Service='Voice';Method='GET';Route='/api/voice/diagnostic';Mutating=$false;Purpose='diagnóstico'},
        [pscustomobject]@{Service='Voice';Method='POST';Route='/api/voice/stt';Mutating=$true;Purpose='STT'},
        [pscustomobject]@{Service='Voice';Method='POST';Route='/api/voice/chat';Mutating=$true;Purpose='chat'},
        [pscustomobject]@{Service='Voice';Method='POST';Route='/api/voice/tts';Mutating=$true;Purpose='TTS'},
        [pscustomobject]@{Service='Voice';Method='POST';Route='/api/voice/talk';Mutating=$true;Purpose='voz streaming'},
        [pscustomobject]@{Service='Voice';Method='POST';Route='/api/voice/reset_session';Mutating=$true;Purpose='reset sessão'},
        [pscustomobject]@{Service='Voice';Method='POST';Route='/api/voice/reload';Mutating=$true;Purpose='reload motores'}
    )
}

function Render-ManualAudit {
    param($Audit,$Schema,$Wom,$Cors,$RouteCoverage)
    Write-Section '12. AUDITORIA DE CONSISTÊNCIA DO MANUAL / WOm / CORS / SQLITE'
    Write-StatusLine 'MANUAL.md' ([string]$Audit.Exists) $(if($Audit.Exists){'OK'}else{'ERROR'})
    Write-StatusLine 'Versão no cabeçalho' ([string]$Audit.HeaderVersion) 'INFO'
    Write-StatusLine 'Versão no rodapé' ([string]$Audit.FooterVersion) 'INFO'
    if($Audit.InconsistentVersionText){Write-StatusLine 'Consistência de versão' 'INCONSISTENTE' 'WARN'}else{Write-StatusLine 'Consistência de versão' 'OK' 'OK'}
    if($Audit.Section8Reference -and -not $Audit.Section8Exists){Write-StatusLine 'Referência seção 8' 'referência quebrada' 'WARN'}
    foreach($n in $Audit.Notes){Write-StatusLine 'Nota manual' (Truncate-Text $n 170) 'WARN'}
    $dbLevel='WARN';$dbText='indisponível';if($Schema.Available){$dbLevel='OK';$dbText=($Schema.Tables -join ', ')};Write-StatusLine 'SQLite tabelas detectadas' (Truncate-Text $dbText 190) $dbLevel
    $corsLevel='WARN';$corsText='não validado';if($Cors.Ok -and $Cors.Allowed){$corsLevel='OK';$corsText='Allow-Origin='+[string]$Cors.Allowed}else{if($Cors.Ok){$corsText='health respondeu, header CORS não confirmado'}else{$corsText='falha no teste CORS'}};Write-StatusLine 'Voice CORS chrome-extension' $corsText $corsLevel
    $womLevel='INFO';if($Wom.Available -and $Wom.Interpretation -like 'divergência*'){$womLevel='WARN'};Write-StatusLine 'WoM' ($Wom.Interpretation) $womLevel
    Write-StatusLine 'Rotas GET documentadas' ([string]$RouteCoverage.ReadOnly) 'INFO'
    if($RouteCoverage.ReadOnlyUncovered.Count -gt 0){Write-StatusLine 'GET não exercitadas' ($RouteCoverage.ReadOnlyUncovered -join ', ') 'WARN'}else{Write-StatusLine 'Cobertura GET documentada' 'OK' 'OK'}
}

function Render-ManualAuditTail {
    param($Manual,$VoiceHealth)
    Write-Section '13. AUDITORIA ESPECÍFICA DE VOZ / LLM HEALTH'
    if($VoiceHealth){
        $body=$VoiceHealth.Body
        if($body){
            foreach($field in @('ok','engineReady','loading','device','llmModel','uptimeS')){
                if($body.PSObject.Properties.Name -contains $field){Write-StatusLine ('VOICE '+$field) (Truncate-Text ([string]$body.$field) 160) 'INFO'}
            }
            if($body.llmHealth){
                foreach($field in @('host','requested_model','active_model','last_error','last_latency_ms')){
                    if($body.llmHealth.PSObject.Properties.Name -contains $field){$lvl='INFO';if($field -eq 'last_error' -and $body.llmHealth.$field){$lvl='WARN'};Write-StatusLine ('LLM '+$field) (Truncate-Text ([string]$body.llmHealth.$field) 170) $lvl}
                }
            }
        }
    }
    Write-StatusLine 'Manualização' 'alterações do sistema devem atualizar MANUAL/changelog' 'INFO'
}

function Render-ComponentInventory {
    param([array]$Extension,[array]$Engine,[array]$Voice)
    Write-Section '15. INVENTÁRIO — EXTENSÃO / ENGINE / VOZ'
    $groups=@(
        [pscustomobject]@{Name='Extensão';Rows=$Extension},
        [pscustomobject]@{Name='Engine';Rows=$Engine},
        [pscustomobject]@{Name='Voice';Rows=$Voice}
    )
    foreach($g in $groups){
        $ok=@($g.Rows | Where-Object {$_.Exists}).Count
        $total=@($g.Rows).Count
        $invLevel='WARN';if($ok -eq $total){$invLevel='OK'}
        Write-StatusLine ('Inventário '+$g.Name) ($ok+' / '+$total+' presentes') $invLevel
        foreach($r in ($g.Rows | Where-Object {-not $_.Exists} | Select-Object -First 12)){
            $name=$r.File; if($r.Component){$name=$r.Component}elseif($r.Module){$name=$r.Module}
            Write-StatusLine ('ausente '+$g.Name) $name 'WARN'
        }
    }
}

function Render-StartupRecovery {
    param([array]$Rows,[array]$Routes)
    Write-Section '16. STARTUP / RECUPERAÇÃO / ROTAS DOCUMENTADAS'
    foreach($r in $Rows){
        $startText='ausente';$startLevel='WARN';if($r.Exists){$startText='presente | '+$r.SizeKB+' KB';$startLevel='OK'}
        Write-StatusLine $r.Name $startText $startLevel
    }
    $mutating=@($Routes | Where-Object {$_.Mutating}).Count
    Write-StatusLine 'Rotas registradas no manual' ([string]$Routes.Count) 'INFO'
    Write-StatusLine 'Rotas mutáveis (não executadas pelo monitor)' ([string]$mutating) 'INFO'
    Write-Host '  Rotas mutáveis são apenas inventariadas; o monitor não dispara POSTs operacionais.' -ForegroundColor DarkGray
}

# ================================================================
# RENDERIZAÇÃO
# ================================================================
function Render-Services {
    param([array]$Rows)
    Write-Section '1. SERVIÇOS OFICIAIS — TCP + HTTP + PAPEL'
    foreach($s in $Rows){
        $text = switch($s.State){
            'ONLINE' { 'ONLINE | HTTP ' + $s.Status + ' | ' + $s.LatencyMs + ' ms' }
            'SLOW' { 'ONLINE | LENTO | HTTP ' + $s.Status + ' | ' + $s.LatencyMs + ' ms' }
            'TCP_ONLY' { 'TCP ONLINE | HTTP SEM RESPOSTA' }
            default { 'OFFLINE | porta ' + $s.Port }
        }
        $level = switch($s.State){ 'ONLINE' {'OK'} 'SLOW' {'WARN'} 'TCP_ONLY' {'WARN'} default {'ERROR'} }
        Write-StatusLine ($s.Name + ' :' + $s.Port) ($text + ' | ' + $s.Role) $level
        if($s.Name -eq 'VOICE' -and $s.Body){
            try {
                if($s.Body.loading -eq $true){Write-StatusLine 'VOICE loading' 'modelo/motores carregando; health pode ficar pronto antes do engine' 'WARN'}
                elseif($s.Body.engineReady -eq $true){Write-StatusLine 'VOICE engineReady' 'true' 'OK'}
                elseif($null -ne $s.Body.engineReady){Write-StatusLine 'VOICE engineReady' 'false' 'WARN'}
            }catch{}
        }
    }
}

function Render-ServiceEndpoints {
    param([array]$Engine,[array]$Bridge,[array]$Voice)
    Write-Section '2. ENDPOINTS OFICIAIS / ORQUESTRADOR / DIAGNÓSTICO'
    foreach($r in (@($Engine)+@($Bridge)+@($Voice))){
        $val='INDISPONÍVEL | '+(Truncate-Text $r.Error 110); if($r.Ok){$val='HTTP '+$r.Status+' | '+$r.LatencyMs+' ms'}
        $epLevel='WARN'; if($r.Ok){$epLevel='OK'}
        Write-StatusLine $r.Name $val $epLevel
    }
}

function Render-System {
    param($System,$Gpu,$Env)
    Write-Section '3. WINDOWS / RECURSOS / HARDWARE'
    Write-StatusLine 'Computador' ([string]$System.ComputerName) 'INFO'
    Write-StatusLine 'Windows' ([string]$System.OS + ' ' + [string]$System.OSVersion) 'INFO'
    Write-StatusLine 'PowerShell' ([string]$System.PowerShell) 'INFO'
    Write-StatusLine 'CPU' ((Truncate-Text ([string]$System.CPU) 85) + ' | logical=' + [string]$System.CPUCores) 'INFO'
    if($null -ne $System.RAMFreePct){
        $lvl='OK'; if($System.RAMFreePct -lt $WarningMemoryFreePercent){$lvl='WARN'}
        Write-StatusLine 'RAM' (($System.RAMUsedGB)+' / '+($System.RAMTotalGB)+' GB usados | livre '+$System.RAMFreeGB+' GB ('+$System.RAMFreePct+'%)') $lvl
    }
    if($null -ne $System.DiskFreeGB){
        $lvl='OK'; if($System.DiskFreeGB -lt $WarningDiskFreeGB){$lvl='WARN'}
        Write-StatusLine 'Disco C:' ($System.DiskFreeGB+' / '+$System.DiskTotalGB+' GB livres') $lvl
    }
    Write-StatusLine 'GPUs detectadas' ([string]$System.GPUCount + ' | ' + ((@($System.GPUs) -join '; '))) 'INFO'
    $smiText='não disponível'; $smiLevel='INFO'; if($Gpu.nvidiaSmi){$smiText='disponível | '+(Truncate-Text $Gpu.nvidiaSmiText 140);$smiLevel='OK'}
    Write-StatusLine 'nvidia-smi' $smiText $smiLevel
    $torchText='Torch não validado no venv detectado'; $torchLevel='INFO'; if($Gpu.TorchVersion){$torchText=$Gpu.TorchVersion+' | CUDA='+$Gpu.TorchCuda}; if($Gpu.TorchCuda){$torchLevel='OK'}
    Write-StatusLine 'Torch CUDA' $torchText $torchLevel
    foreach($k in $Env.PSObject.Properties.Name){Write-StatusLine $k ([string]$Env.$k) 'INFO'}
}

function Render-Processes {
    param([array]$Rows)
    Write-Section '4. PROCESSOS AURA / IA / NAVEGADOR'
    if($Rows.Count -eq 0){Write-StatusLine 'Processos relevantes' 'nenhum processo encontrado' 'WARN';return}
    $Rows | Select-Object -First 18 Id,Name,RAM_MB,CPU_s | Format-Table -AutoSize | Out-Host
}

function Render-PortOwners {
    param([array]$Rows)
    Write-Section '5. PORTAS / OWNERS'
    if($Rows.Count -eq 0){Write-StatusLine 'Portas' 'nenhum listener detectado' 'WARN';return}
    foreach($r in $Rows){Write-StatusLine (':' + $r.Port) ('PID='+$r.PID+' | '+$r.Process+' | '+$r.LocalAddress) 'INFO'}
}

function Render-Ollama {
    param($Ollama)
    Write-Section '6. OLLAMA / MODELOS / FALLBACK'
    if(-not $Ollama.Online){Write-StatusLine 'Ollama' 'OFFLINE | '+(Truncate-Text $Ollama.Error 150) 'ERROR';return}
    Write-StatusLine 'Ollama API' ('ONLINE | '+$Ollama.LatencyMs+' ms') 'OK'
    if($Ollama.Models.Count -eq 0){Write-StatusLine 'Modelos instalados' 'nenhum modelo retornado' 'WARN';return}
    Write-Host '  Modelos:' -ForegroundColor White
    foreach($m in $Ollama.Models){Write-Host ('    - {0} | {1} GB | {2}' -f $m.Name,$m.SizeGB,$m.Modified) -ForegroundColor DarkGray}
    $ollamaLevel='WARN'; if($Ollama.HasRecommended){$ollamaLevel='OK'}
    Write-StatusLine 'Fallbacks compatíveis encontrados' (($Ollama.PreferredInstalled -join ', ')) $ollamaLevel
}

function Render-VenvManifest {
    param($Venv,$Manifest)
    Write-Section '7. EXTENSÃO / MANIFEST / PYTHON / VENV'
    $venvText='ausente';$venvLevel='WARN';if($Venv.Exists){$venvText='existente';$venvLevel='OK'}
    Write-StatusLine 'Engine venv' $venvText $venvLevel
    $pyText='ausente';$pyLevel='ERROR';if($Venv.PythonExists){$pyText=$Venv.Python;$pyLevel='OK'}
    Write-StatusLine 'Python do venv' $pyText $pyLevel
    $pipText='ausente';$pipLevel='WARN';if($Venv.PipExists){$pipText='presente';$pipLevel='OK'}
    Write-StatusLine 'pip do venv' $pipText $pipLevel
    if($Venv.Imports.Count -gt 0){foreach($i in $Venv.Imports){$iLevel='WARN';if($i -match '=OK$'){$iLevel='OK'};Write-StatusLine 'import' $i $iLevel}}
    $manifestExistsText='não';$manifestExistsLevel='WARN';if($Manifest.Exists){$manifestExistsText='sim';$manifestExistsLevel='OK'}
    Write-StatusLine 'Manifest existe' $manifestExistsText $manifestExistsLevel
    if($Manifest.Exists){
        $manifestValidText='não | '+$Manifest.Error;$manifestValidLevel='ERROR';if($Manifest.Valid){$manifestValidText='sim';$manifestValidLevel='OK'}
        Write-StatusLine 'Manifest JSON válido' $manifestValidText $manifestValidLevel
        Write-StatusLine 'Manifest version' ([string]$Manifest.Version) 'INFO'
        $mv3Text='não';$mv3Level='ERROR';if($Manifest.MV3){$mv3Text='sim';$mv3Level='OK'}
        Write-StatusLine 'MV3' $mv3Text $mv3Level
        $spText='não';$spLevel='WARN';if($Manifest.SidePanel){$spText='sim';$spLevel='OK'}
        Write-StatusLine 'Side Panel declarado' $spText $spLevel
    }
}

function Render-Persistence {
    param($Persistence)
    Write-Section '8. PERSISTÊNCIA / RUNTIME / FEEDS'
    foreach($f in $Persistence.Files){
        $lvl='MUTED'; if($f.Exists){$lvl='OK'}elseif($f.Severity -eq 'WARN'){$lvl='WARN'}
        $v='ausente'; if($f.Exists){$v=$f.SizeKB+' KB | idade '+([math]::Round($f.AgeSec,1))+' s'}
        Write-StatusLine $f.Label $v $lvl
    }
    if($Persistence.Runtime.Count -gt 0){
        Write-Host '  Runtime recente:' -ForegroundColor White
        $Persistence.Runtime | Select-Object -First 10 @{N='Arquivo';E={(Split-Path $_.FullName -Leaf)}},LastWriteTime,SizeKB | Format-Table -AutoSize | Out-Host
    } else {Write-StatusLine 'Runtime' 'sem arquivos' 'WARN'}
    foreach($f in $Persistence.Feeds){$feedLvl='WARN';if($f.AgeSec -le $FeedStaleSeconds){$feedLvl='OK'};Write-StatusLine ('Feed '+(Split-Path $f.Path -Leaf)) ($f.SizeKB+' KB | '+([math]::Round($f.AgeSec,1))+' s') $feedLvl}
}

function Render-DataIntegrity {
    param($Data)
    Write-Section '9. INTEGRIDADE DOS DADOS / FAIL-CLOSED'
    if(-not $Data.HasData){Write-StatusLine 'Feed JSONL' 'sem registro válido recente' 'WARN';return}
    Write-StatusLine 'Fixture' ([string]$Data.FixtureId) 'INFO'
    Write-StatusLine 'Partida' (([string]$Data.Home)+' x '+([string]$Data.Away)) 'INFO'
    Write-StatusLine 'Minuto' ([string]$Data.Minute) 'INFO'
    $freshText='STALE > '+$FeedStaleSeconds+' s';$freshLevel='WARN';if($Data.Fresh){$freshText='OK <= '+$FeedStaleSeconds+' s';$freshLevel='OK'}
    Write-StatusLine 'Freshness' $freshText $freshLevel
    $negLevel='ERROR';if($Data.NegativeCount -eq 0){$negLevel='OK'}
    Write-StatusLine 'Valores negativos' ([string]$Data.NegativeCount) $negLevel
    if($null -eq $Data.Reconciled){Write-StatusLine 'Reconciliação corners' 'não determinável pelo formato atual do feed' 'INFO'}
    elseif($Data.Reconciled){Write-StatusLine 'Reconciliação corners' 'OK' 'OK'}
    else{Write-StatusLine 'Reconciliação corners' 'CONFLITO — entrada deve permanecer BLOCK' 'ERROR'}
    $gateLevel='WARN';if($Data.Status -eq 'OK'){$gateLevel='OK'}elseif($Data.Status -eq 'BLOCK'){$gateLevel='ERROR'}
    Write-StatusLine 'Gate de integridade' $Data.Status $gateLevel
    Write-StatusLine 'Nota' (Truncate-Text $Data.Note 160) 'INFO'
}

function Render-Analysis {
    param($Analysis,$Risk,$Orch)
    Write-Section '10. ANÁLISE / EXPLICABILIDADE / RISK / ORQUESTRADOR'
    if(-not $Analysis.Available){Write-StatusLine 'Análise atual' 'indisponível' 'WARN'}
    else {
        Write-StatusLine 'Fixture' ([string]$Analysis.FixtureId) 'INFO'
        Write-StatusLine 'Decision' ([string]$Analysis.Decision) 'INFO'
        $riskGateLevel='INFO';if([string]$Analysis.RiskGate -match 'BLOCK|FAIL|DENY'){$riskGateLevel='ERROR'}
        Write-StatusLine 'Risk gate' ([string]$Analysis.RiskGate) $riskGateLevel
        Write-StatusLine 'Edge' ([string]$Analysis.Edge) 'INFO'
        Write-StatusLine 'Uncertainty' ([string]$Analysis.Uncertainty) 'INFO'
        Write-StatusLine 'Reason' (Truncate-Text ([string]$Analysis.Reason) 180) 'INFO'
        if($Analysis.Market){Write-StatusLine 'Market/WoM' (Truncate-Text (ConvertTo-SafeJson $Analysis.Market) 180) 'INFO'}
        if($Analysis.DataIntegrity){Write-StatusLine 'data_integrity' (Truncate-Text (ConvertTo-SafeJson $Analysis.DataIntegrity) 180) 'INFO'}
        $classLevel='INFO';if($Risk.State -eq 'BLOCK'){$classLevel='ERROR'}elseif($Risk.State -eq 'BUY_PRESENT_BUT_VALIDATE'){$classLevel='WARN'}
        Write-StatusLine 'Classificação' $Risk.State $classLevel
    }
    $orchText='indisponível';$orchLevel='WARN';if($Orch.Ok){$orchText='snapshot disponível | '+$Orch.LatencyMs+' ms';$orchLevel='OK'}
    Write-StatusLine 'Orchestrator' $orchText $orchLevel
}

function Render-LogsEvents {
    param([array]$Errors,$Event)
    Write-Section '11. AUDITORIA / ERROS / OBSERVABILIDADE'
    if($Errors.Count -eq 0){Write-StatusLine 'Erros recentes' 'nenhum padrão crítico encontrado nos logs monitorados' 'OK'}
    else {
        Write-StatusLine 'Erros recentes' ([string]$Errors.Count+' ocorrência(s) exibíveis') 'WARN'
        foreach($e in $Errors){Write-Host ('  [{0}] {1}' -f $e.File,$e.Line) -ForegroundColor Red}
    }
    if($Event.Ok){
        Write-StatusLine 'Último evento estruturado' $Event.Raw 'INFO'
    } else {Write-StatusLine 'runtime_events.jsonl' 'sem evento JSON válido recente' 'WARN'}
}

function Render-WindowsDiagnostics {
    param($Diag,$Reports)
    Write-Section '12. DIAGNÓSTICO WINDOWS — SOMENTE LEITURA'
    $fwText='indisponíveis';if($Diag.FirewallCmd){$fwText='disponíveis'}
    Write-StatusLine 'Firewall cmdlets' $fwText 'INFO'
    if($Diag.FirewallProfiles){
        foreach($p in $Diag.FirewallProfiles){Write-StatusLine ('Firewall '+$p.Name) ('Enabled='+$p.Enabled+' | In='+$p.DefaultInboundAction+' | Out='+$p.DefaultOutboundAction) 'INFO'}
    }
    $defText='indisponíveis';if($Diag.DefenderCmd){$defText='disponíveis'}
    Write-StatusLine 'Defender cmdlets' $defText 'INFO'
    if($null -ne $Diag.DefenderRealtime){$defrtLevel='WARN';if($Diag.DefenderRealtime){$defrtLevel='OK'};Write-StatusLine 'Defender realtime' ([string]$Diag.DefenderRealtime) $defrtLevel}
    Write-StatusLine 'DISM disponível' ([string]$Diag.DISM) 'INFO'
    Write-StatusLine 'SFC disponível' ([string]$Diag.SFC) 'INFO'
    if($Reports.Count -gt 0){
        Write-Host '  Relatórios de diagnóstico existentes:' -ForegroundColor White
        $Reports | Format-Table -AutoSize | Out-Host
    }
}

function Render-Environment {
    param($Expected,$VersionInfo,$Audit)
    Write-Section '13. ESTRUTURA ESPERADA / VERSIONAMENTO / AUDITORIA DO MONITOR'
    foreach($e in $Expected){
        $p=Join-Path $Root $e
        $exists=Test-Path -LiteralPath $p
        $eText='ausente';$eLevel='WARN';if($exists){$eText='presente';$eLevel='OK'}
        Write-StatusLine $e $eText $eLevel
    }
    $manualText='não localizada no cabeçalho';if($VersionInfo){$manualText=$VersionInfo}
    Write-StatusLine 'Versão do MANUAL' $manualText 'INFO'
    $auditText='sem arquivo ainda';if($Audit.Exists){$auditText='ativo | '+$Audit.Entries+' registros'}
    Write-StatusLine 'Auto-auditoria monitor' $auditText 'INFO'
}

function Render-Global {
    param($Global,$CriticalCount,$DegradedCount)
    Write-Section '14. RESUMO EXECUTIVO'
    $level=$Global.Level
    $color='OK'; if($level -eq 'CRITICAL' -or $level -eq 'BLOCKED'){$color='ERROR'}elseif($level -in @('DEGRADED','PARTIAL')){$color='WARN'}
    Write-StatusLine 'STATUS AURA' ($level+' | '+$Global.Reason) $color
    $criticalLevel='ERROR';if($CriticalCount -eq 0){$criticalLevel='OK'}
    Write-StatusLine 'Serviços críticos offline' ([string]$CriticalCount) $criticalLevel
    $degradedLevel='WARN';if($DegradedCount -eq 0){$degradedLevel='OK'}
    Write-StatusLine 'Serviços degradados' ([string]$DegradedCount) $degradedLevel
    Write-StatusLine 'Princípio operacional' 'FAIL-CLOSED | PAPER-FIRST | sem stake real neste monitor' 'INFO'
    Write-StatusLine 'Ação automática do monitor' 'NENHUMA — somente leitura' 'INFO'
}

# ================================================================
# LOOP PRINCIPAL
# ================================================================
while ($true) {
    try {
        Clear-Host
        Write-Host ('AURA QUANT-X — LIVE MONITOR PRO | ' + $ScriptVersion) -ForegroundColor Cyan
        Write-Host ('Root: ' + $Root + ' | ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' | refresh=' + $RefreshSeconds + 's') -ForegroundColor DarkGray

        if(-not(Test-Path -LiteralPath $Root)){
            Write-Section 'FALHA DE RAIZ'
            Write-StatusLine 'C:\aura' 'não encontrado' 'ERROR'
            Start-Sleep -Seconds $RefreshSeconds
            continue
        }

        $serviceRows=@()
        foreach($svc in $Services){$serviceRows += Get-ServiceSnapshot -Service $svc}
        $criticalCount=@($serviceRows | Where-Object {$_.Critical -and $_.State -eq 'OFFLINE'}).Count
        $degradedCount=@($serviceRows | Where-Object {$_.State -in @('SLOW','TCP_ONLY')}).Count

        $engineEndpointRows=@()
        $bridgeEndpointRows=@()
        $voiceEndpointRows=@()
        foreach($ep in $EngineEndpoints){$engineEndpointRows += Get-EndpointStatus -Endpoint $ep}
        foreach($ep in $BridgeEndpoints){$bridgeEndpointRows += Get-EndpointStatus -Endpoint $ep}
        foreach($ep in $VoiceEndpoints){$voiceEndpointRows += Get-EndpointStatus -Endpoint $ep}

        $system=Get-SystemSnapshot
        $gpu=Get-GpuRuntimeSnapshot
        $env=Get-PowerShellEnvironmentSnapshot
        $processes=Get-ProcessSnapshot
        $portOwners=Get-PortOwners
        $ollama=Get-OllamaSnapshot
        $manifest=Get-ManifestSnapshot
        $venv=Get-VenvSnapshot
        $persistence=Get-PersistenceSnapshot
        $dataIntegrity=Get-DataIntegritySnapshot
        $analysis=Get-AnalysisSnapshot
        $risk=Get-RiskPresentation -Analysis $analysis
        $orchestrator=Get-OrchestratorSnapshot
        $errors=Get-RecentLogErrors
        $event=Get-LastStructuredEvent
        $winDiag=Get-WindowsDiagnosticsSnapshot
        $reports=@(Get-ExistingDiagnosticReports)
        $audit=Get-AuditFileSnapshot
        $manualVersion=Get-FileVersionText -Path (Join-Path $Root 'MANUAL.md')
        $feeds=Get-LiveFeedSnapshot
        $extensionInventory=Get-ComponentInventorySnapshot
        $engineInventory=Get-EngineModuleInventorySnapshot
        $voiceInventory=Get-VoiceInventorySnapshot
        $startupRecovery=Get-StartupRecoverySnapshot
        $routeRegistry=Get-DocumentedRouteRegistry
        $routeCoverage=Get-RouteCoverageAudit
        $manualAudit=Get-ManualAuditSnapshot
        $sqliteSchema=Get-SqliteSchemaSnapshot
        $wom=Get-WomSnapshot -Analysis $analysis
        $voiceCors=Test-ExtensionCorsHealth
        $global=Get-GlobalState -Services $serviceRows -DataIntegrity $dataIntegrity -Venv $venv -Manifest $manifest -Ollama $ollama -Feeds $feeds -Analysis $analysis

        $snapshot=@{
            timestamp=(Get-Date).ToString('o')
            Services=@($serviceRows | Select-Object Name,Port,State,Status,LatencyMs,Critical)
            GlobalState=$global.Level
            CriticalErrors=$criticalCount
        }
        Write-SelfAudit -Snapshot $snapshot

        Render-Services -Rows $serviceRows
        Render-ServiceEndpoints -Engine $engineEndpointRows -Bridge $bridgeEndpointRows -Voice $voiceEndpointRows
        Render-System -System $system -Gpu $gpu -Env $env
        Render-Processes -Rows $processes
        Render-PortOwners -Rows $portOwners
        Render-Ollama -Ollama $ollama
        Render-VenvManifest -Venv $venv -Manifest $manifest
        Render-Persistence -Persistence $persistence
        Render-DataIntegrity -Data $dataIntegrity
        Render-Analysis -Analysis $analysis -Risk $risk -Orch $orchestrator
        Render-LogsEvents -Errors $errors -Event $event
        Render-WindowsDiagnostics -Diag $winDiag -Reports $reports
        Render-Environment -Expected $ExpectedPaths -VersionInfo $manualVersion -Audit $audit
        Render-ManualAudit -Audit $manualAudit -Schema $sqliteSchema -Wom $wom -Cors $voiceCors -RouteCoverage $routeCoverage
        Render-ManualAuditTail -Manual $manualAudit -VoiceHealth ($serviceRows | Where-Object {$_.Name -eq 'VOICE'} | Select-Object -First 1)
        Render-ComponentInventory -Extension $extensionInventory -Engine $engineInventory -Voice $voiceInventory
        Render-StartupRecovery -Rows $startupRecovery -Routes $routeRegistry
        Render-Global -Global $global -CriticalCount $criticalCount -DegradedCount $degradedCount

        Write-Section 'CONTROLES'
        Write-Host '  Ctrl+C = sair | monitor sem escrita funcional no AURA | nenhuma ação corretiva automática' -ForegroundColor DarkGray
        Write-Host '  Gates observados: integridade/freshness → analysis → risk_gate → WoM → observabilidade' -ForegroundColor DarkGray
        Write-Host '  Referência funcional: MANUAL.md v12.6.17' -ForegroundColor DarkGray
        Start-Sleep -Seconds $RefreshSeconds
    }
    catch {
        Write-Host ''
        Write-Host ('MONITOR EXCEPTION: ' + (Truncate-Text $_.Exception.Message 220)) -ForegroundColor Red
        Write-Host 'O loop continuará sem executar ações corretivas.' -ForegroundColor Yellow
        Start-Sleep -Seconds $RefreshSeconds
    }
}

# ================================================================================
# BUILD AUDIT: 2026-09-04 | v4 audit-max | manual 12.6.17 reconciliado | correções adicionais aplicadas
# ================================================================================
