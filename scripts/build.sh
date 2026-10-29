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
for review in flagged all; do
  control="paper-control-$stamp-$review"
  extra=()
  if [ "$review" = flagged ]; then extra=(--cancel-stage stage3); fi
  if ! docker run --name "$control" --network none --read-only --tmpfs /tmp:size=512m,mode=1777 --mount "type=volume,source=$control-data,target=/data" --entrypoint /opt/venvs/app/bin/python "$image" -m app.smoke --review "$review" "${extra[@]}"; then
    docker cp "$control:/data/." "artifacts/$control"
    echo "Qualification failed; candidate container and data retained: $control" >&2
    exit 1
  fi
  docker cp "$control:/data/." "artifacts/$control"
done
cp composition/pipeline.lock.json "artifacts/pipeline.previous-$stamp.lock.json"
docker image inspect "$image" > "artifacts/image-$stamp.json"
cp .build/candidate.lock.json composition/pipeline.lock.json
docker tag "$image" paper-orchestrator:local
echo 'Both CPU controls passed. Start with: docker compose up -d'
