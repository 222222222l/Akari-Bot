#!/usr/bin/env bash
set -euo pipefail
MUSIC_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$MUSIC_ROOT"
export UV_CACHE_DIR="$MUSIC_ROOT/.uv-cache"
export HF_HOME="$MUSIC_ROOT/hf-cache"
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-600}"
export UV_CONCURRENT_DOWNLOADS="${UV_CONCURRENT_DOWNLOADS:-3}"
MODE="${1:-all}"
case "$MODE" in all|svc|yue|sheetsage) ;; *) echo 'Usage: bash music/setup.sh [all|svc|yue|sheetsage]' >&2; exit 2;; esac
command -v uv >/dev/null
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
mkdir -p vendor models songs wheels
checkout() {
  local repo="$1" destination="$2" revision="$3"
  if [[ ! -d "$destination" ]]; then
    git init "$destination"
    git -C "$destination" remote add origin "$repo"
    git -C "$destination" fetch --depth 1 origin "$revision"
    git -C "$destination" checkout --detach FETCH_HEAD
  fi
  [[ "$(git -C "$destination" rev-parse HEAD)" == "$revision" ]] || {
    echo "Unexpected revision in $destination; refusing to replace existing checkout" >&2; exit 1;
  }
  [[ -z "$(git -C "$destination" status --porcelain)" ]] || {
    echo "Modified checkout $destination; preserve changes before installation" >&2; exit 1;
  }
}
if [[ "$MODE" == all || "$MODE" == svc ]]; then
  checkout https://github.com/Plachtaa/seed-vc.git vendor/seed-vc 51383efd921027683c89e5348211d93ff12ac2a8
  [[ -x .venv-svc/bin/python ]] || uv venv --python 3.11 .venv-svc
  uv pip install --python .venv-svc/bin/python -r requirements-svc.txt --find-links wheels
  uv pip freeze --python .venv-svc/bin/python > installed-svc.txt
  .venv-svc/bin/python - <<'PY'
from huggingface_hub import hf_hub_download
for name in ('DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth',
             'config_dit_mel_seed_uvit_whisper_base_f0_44k.yml'):
    hf_hub_download('Plachta/Seed-VC', name, revision='257283f9f41585055e8f858fba4fd044e5caed6e',
                    local_dir='vendor/seed-vc/checkpoints')
PY
fi
if [[ "$MODE" == all || "$MODE" == yue ]]; then
  checkout https://github.com/multimodal-art-projection/YuE.git vendor/YuE 0edaf2f4053ef4731334b8329834b107977f9637
  [[ -x .venv-yue/bin/python ]] || uv venv --python 3.12 .venv-yue
  uv pip install --python .venv-yue/bin/python ./vendor/YuE --find-links wheels
  uv pip freeze --python .venv-yue/bin/python > installed-yue.txt
fi
if [[ "$MODE" == all || "$MODE" == sheetsage ]]; then
  [[ -x .venv-sheetsage/bin/python ]] || uv venv --python 3.11 .venv-sheetsage
  uv pip install --python .venv-sheetsage/bin/python huggingface-hub==0.36.0
  .venv-sheetsage/bin/huggingface-cli download m-a-p/SheetSage2 \
    --revision cd2f39b3b807b81f6e32bbb8ea6011da40f77649 --local-dir models/SheetSage2
  uv pip install --python .venv-sheetsage/bin/python -r models/SheetSage2/requirements.txt --find-links wheels
  uv pip freeze --python .venv-sheetsage/bin/python > installed-sheetsage.txt
fi
echo 'Installed. Review music/config.json GPU selection; model weights download on first inference.'
