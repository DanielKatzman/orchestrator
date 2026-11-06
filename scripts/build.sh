#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .build artifacts
if [ "$#" -gt 0 ]; then
  docker build -f docker/Resolver.Dockerfile -t paper-resolver:local .
  docker run --rm --mount "type=bind,source=$PWD,target=/workspace" paper-resolver:local "$@"
else
  cp composition/pipeline.lock.json .build/candidate.lock.json
fi
stamp=$(date -u +%Y%m%d%H%M%S)
image="paper-orchestrator:candidate-$stamp"
docker build --platform linux/amd64 --build-arg COMPONENT_LOCK=.build/candidate.lock.json -f docker/Dockerfile -t "$image" .
for scenario_review in separate-flagged separate-all multi-all empty-all; do
  scenario=${scenario_review%-*}
  review=${scenario_review#*-}
  control="paper-control-$stamp-$scenario_review"
  extra=()
  if [ "$review" = flagged ]; then extra=(--cancel-stage stage3); fi
  if ! docker run --name "$control" --network none --read-only --tmpfs /tmp:size=512m,mode=1777 --mount "type=volume,source=$control-data,target=/data" --entrypoint /opt/venvs/app/bin/python "$image" -m app.smoke --review "$review" --scenario "$scenario" "${extra[@]}"; then
    docker cp "$control:/data/." "artifacts/$control"
    echo "Qualification failed; candidate container and data retained: $control" >&2
    exit 1
  fi
  docker cp "$control:/data/." "artifacts/$control"
  docker rm "$control"
  docker volume rm "$control-data"
done
cp composition/pipeline.lock.json "artifacts/pipeline.previous-$stamp.lock.json"
docker image inspect "$image" > "artifacts/image-$stamp.json"
cp .build/candidate.lock.json composition/pipeline.lock.json
if docker image inspect paper-orchestrator:local >/dev/null 2>&1; then docker tag paper-orchestrator:local "paper-orchestrator:previous-$stamp"; fi
docker tag "$image" paper-orchestrator:local
echo 'All four CPU controls passed. Start with: docker compose up -d'
