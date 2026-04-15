#!/usr/bin/env bash
# trading-system/scripts/gen_proto.sh
#
# Regenerate Python gRPC stubs from proto/trading.proto.
#
# Uses grpcio-tools==1.67.1 + protobuf==5.28.3 so the output is always
# compatible with the pinned requirements.txt versions in the Docker image.
# (grpcio-tools 1.78+ embeds protobuf 6.x in the gencode header, which causes
#  a major-version mismatch at import time when protobuf==5.28.3 is installed.)
#
# Usage:
#   bash scripts/gen_proto.sh
#
# Output: strategy/src/bridge/trading_pb2.py + trading_pb2_grpc.py
#
# Re-run whenever proto/trading.proto changes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENV_DIR="/tmp/proto_regen_venv"
GRPC_TOOLS_VERSION="1.67.1"
PROTOBUF_VERSION="5.28.3"

log() { echo "[gen_proto] $*"; }

# ── Create/reuse isolated venv ─────────────────────────────────────────────────
if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log "Creating venv at ${VENV_DIR}…"
    python3 -m venv "${VENV_DIR}"
fi

INSTALLED_TOOLS=$("${VENV_DIR}/bin/pip" show grpcio-tools 2>/dev/null | grep ^Version | awk '{print $2}' || echo "none")
if [[ "${INSTALLED_TOOLS}" != "${GRPC_TOOLS_VERSION}" ]]; then
    log "Installing grpcio-tools==${GRPC_TOOLS_VERSION} + protobuf==${PROTOBUF_VERSION}…"
    "${VENV_DIR}/bin/pip" install --quiet \
        "grpcio-tools==${GRPC_TOOLS_VERSION}" \
        "protobuf==${PROTOBUF_VERSION}"
fi

# ── Run protoc ─────────────────────────────────────────────────────────────────
log "Regenerating pb2 files from proto/trading.proto…"
"${VENV_DIR}/bin/python" -m grpc_tools.protoc \
    --proto_path="${ROOT}/proto" \
    --python_out="${ROOT}/strategy/src/bridge" \
    --grpc_python_out="${ROOT}/strategy/src/bridge" \
    "${ROOT}/proto/trading.proto"

# grpcio-tools <=1.67.x generates `import trading_pb2` (absolute).
# Fix to a relative import so it works when the bridge dir is a package.
sed -i 's/^import trading_pb2 as trading__pb2$/from . import trading_pb2 as trading__pb2/' \
    "${ROOT}/strategy/src/bridge/trading_pb2_grpc.py"

log "Done."
log "  → strategy/src/bridge/trading_pb2.py"
log "  → strategy/src/bridge/trading_pb2_grpc.py"

# ── Quick smoke test ───────────────────────────────────────────────────────────
# Import via the package path (strategy/src) so relative imports in pb2_grpc work.
"${VENV_DIR}/bin/python" -c "
import sys
sys.path.insert(0, '${ROOT}/strategy/src')
from bridge import trading_pb2, trading_pb2_grpc
req = trading_pb2.SignalRequest(strategy_id='smoke', symbol='AAPL', direction='BUY', score=0.8)
assert req.symbol == 'AAPL'
print('[gen_proto] Smoke test passed — pb2 files are valid.')
"
