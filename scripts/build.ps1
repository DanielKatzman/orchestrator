param(
    [switch]$LatestCompatible,
    [string[]]$Stage = @(),
    [switch]$Candidate
)
$ErrorActionPreference = 'Stop'
$taskWorkspace = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $taskWorkspace
try {
    New-Item -ItemType Directory -Path '.build','artifacts' -Force | Out-Null
    if ($LatestCompatible -or $Stage.Count -gt 0) {
        docker build -f docker/Resolver.Dockerfile -t paper-resolver:local .
        if ($LASTEXITCODE) { throw 'Resolver image build failed' }
        $taskResolveArgs = @('run','--rm','--mount',"type=bind,source=$taskWorkspace,target=/workspace",'paper-resolver:local','--latest-compatible')
        foreach ($taskSelection in $Stage) { $taskResolveArgs += @('--set',$taskSelection) }
        if ($Candidate) { $taskResolveArgs += '--candidate' }
        & docker @taskResolveArgs
        if ($LASTEXITCODE) { throw 'Compatible version resolution failed; current lock was retained' }
    } else {
        Copy-Item -LiteralPath 'composition/pipeline.lock.json' -Destination '.build/candidate.lock.json'
    }
    $taskBuild = [DateTime]::UtcNow.ToString('yyyyMMddHHmmss')
    $taskImage = "paper-orchestrator:candidate-$taskBuild"
    docker build --platform linux/amd64 --build-arg COMPONENT_LOCK=.build/candidate.lock.json -f docker/Dockerfile -t $taskImage .
    if ($LASTEXITCODE) { throw 'Candidate image build failed; current image was retained' }
    foreach ($taskReview in @('flagged','all')) {
        $taskControl = "paper-control-$taskBuild-$taskReview"
        $taskVolume = "$taskControl-data"
        $taskSmokeArgs = @('run','--name',$taskControl,'--network','none','--read-only','--tmpfs','/tmp:size=512m,mode=1777','--mount',"type=volume,source=$taskVolume,target=/data",'--entrypoint','/opt/venvs/app/bin/python',$taskImage,'-m','app.smoke','--review',$taskReview)
        if ($taskReview -eq 'flagged') { $taskSmokeArgs += @('--cancel-stage','stage3') }
        & docker @taskSmokeArgs
        $taskExit = $LASTEXITCODE
        docker cp "${taskControl}:/data/." "artifacts/$taskControl"
        if ($taskExit) { throw "Integration control failed. Candidate container/data retained: $taskControl" }
    }
    if (Test-Path -LiteralPath 'composition/pipeline.lock.json') {
        Copy-Item -LiteralPath 'composition/pipeline.lock.json' -Destination "artifacts/pipeline.previous-$taskBuild.lock.json"
    }
    docker image inspect $taskImage | Set-Content -LiteralPath "artifacts/image-$taskBuild.json" -Encoding utf8
    Copy-Item -LiteralPath '.build/candidate.lock.json' -Destination 'composition/pipeline.lock.json'
    docker tag $taskImage paper-orchestrator:local
    if ($LASTEXITCODE) { throw 'Image promotion failed' }
    Write-Output 'Both CPU controls passed. Start the application with: docker compose up -d'
} finally { Pop-Location }
