#!/usr/bin/env bash
# Deploy app/ and tests/ to the EC2 server, keeping the previous app for rollback.
#   ./deploy.sh            deploy the working tree
#   ./deploy.sh rollback   restore the previous app
set -euo pipefail
HOST=${GEOAI_HOST:-ec2-user@32.237.40.94}
KEY=${GEOAI_KEY:-~/.ssh/geoai.pem}
remote() { ssh -i "$KEY" "$HOST" "$@"; }

if [ "${1:-}" = rollback ]; then
  remote 'test -d /opt/geoai/app.prev && rm -rf /opt/geoai/app && mv /opt/geoai/app.prev /opt/geoai/app && sudo systemctl restart geoai'
  echo "rolled back"
  exit
fi

remote 'rm -rf /opt/geoai/app.prev && cp -a /opt/geoai/app /opt/geoai/app.prev'
COPYFILE_DISABLE=1 tar czf - --no-xattrs --exclude=__pycache__ --exclude=tests/results app tests pyproject.toml uv.lock \
  | remote 'tar xzf - -C /opt/geoai && cd /opt/geoai && UV_PYTHON_INSTALL_DIR=/opt/uvpython uv sync --frozen --no-dev -q && sudo systemctl restart geoai'
for _ in $(seq 20); do remote 'curl -sf localhost:8000/health' >/dev/null 2>&1 && break; sleep 2; done
remote 'curl -sf localhost:8000/health' && echo " deployed $(git rev-parse --short HEAD)$(git diff --quiet || echo '+uncommitted')"
