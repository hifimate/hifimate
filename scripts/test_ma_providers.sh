#!/usr/bin/env bash
# Run the bundled Music Assistant provider tests (tests/providers/<domain>) inside
# the Music Assistant image the installer uses: they import MA itself.
# Usage: scripts/test_ma_providers.sh [IMAGE] [DOMAIN...]
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
image="${1:-$(sed -n 's/^MUSIC_ASSISTANT_IMAGE=//p' "$root/music-assistant/.env.example")}"
shift || true
domains=("$@")
[ "${#domains[@]}" -gt 0 ] || domains=(bilibili migu)
status=0
for domain in "${domains[@]}"; do
    provider="$root/music-assistant/providers/$domain"
    tests="$root/tests/providers/$domain"
    [ -d "$provider" ] && [ -d "$tests" ] || { echo "no provider or tests for $domain" >&2; exit 2; }
    requirements=$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["requirements"]))' "$provider/manifest.json")
    echo "== $domain ($image)"
    docker run --rm --entrypoint /bin/sh -e DOMAIN="$domain" -e REQUIREMENTS="$requirements" \
        -v "$provider:/opt/provider:ro" -v "$tests:/opt/tests:ro" "$image" -c '
        set -e
        providers=$(/app/venv/bin/python -c "import music_assistant.providers as p; print(p.__path__[0])")
        cp -r /opt/provider "$providers/$DOMAIN"
        /app/venv/bin/uv pip install -q --python /app/venv/bin/python pytest pytest-asyncio $REQUIREMENTS
        mkdir -p /tmp/run/tests/providers
        cp -r /opt/tests "/tmp/run/tests/providers/$DOMAIN"
        touch /tmp/run/tests/__init__.py /tmp/run/tests/providers/__init__.py
        cd /tmp/run && /app/venv/bin/python -m pytest -q -p no:cacheprovider -o asyncio_mode=auto tests' || status=1
done
exit "$status"
