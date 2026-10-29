#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------
# Dillflix multi-tier RTMP fanout (MPP + VA)
#
# Goal (VA mode):
#   - Capture is effectively 60fps on this device
#   - Tier0: 1080p60
#   - Tiers1-5: 30fps (downsampled from 60 with videorate drop-only)
#   - No MJPG support (YUY2 only)
#
# Usage:
#   ./stream_fanout.sh va
#   ./stream_fanout.sh mpp
#   ./stream_fanout.sh --dry-run va
#
# Back-compat alias:
#   ./stream_fanout.sh vaapi   # same as "va"
#
# Debug save (tee one muxed FLV output to disk):
#   ./stream_fanout.sh va --debug-save 0 /var/tmp/tier0_debug.flv
# ------------------------------------------------------------

usage() {
  cat <<'EOF'
Usage: dillflix_gstreamer.sh [--dry-run] [va|vaapi|mpp] [--debug-save <tier> <path>]

Options:
  --dry-run                   Print the command that would be executed, then exit.
  --debug-save <tier> <path>  Save one tier's muxed FLV output to a local file (e.g., tier 0 -> /tmp/t0.flv).

Env overrides:
  VIDEO_DEV            (mpp default /dev/video1)
  VIDEO_DEV_VA         (va default /dev/video0)
  ALSA_DEV             (default hw:CARD=Device,DEV=0)

Audio timing / caps lock:
  AUDIO_RATE           (default 48000)
  AUDIO_CHANNELS       (default 2)

VA capture / fps:
  VIDEO_W              (default 1920)
  VIDEO_H              (default 1080)
  CAPTURE_FPS          (default 60)   # what the device effectively outputs
  TOP_FPS              (default 60)   # tier0 fps
  LOW_FPS              (default 30)   # tiers1-5 fps (derived via videorate drop-only)

VA encoder:
  VA_ENCODER           (vah264lpenc|vah264enc, default auto)
  VA_TARGET_USAGE      (default 7)

RTMP:
  RTMP_HOST            (default 192.168.0.116)
  RTMP_PORT            (default 6968)
  RTMP_APP             (default dillflix-live)
  STREAM               (default sports0)

Optional public broadcast overlay (disabled unless URL is set):
  DILLFLIX_OVERLAY_URL          Full controller overlay endpoint for this device
  DILLFLIX_OVERLAY_DEVICE       Required explicit controller device ID
  DILLFLIX_OVERLAY_NAME         Fallback public name (default Dillflix)
  DILLFLIX_OVERLAY_HEADERS_FILE Optional JSON request headers file; never printed

Stay-live / leaky queue tuning (video branches after tees/scales):
  LEAK_TIME_NS         (default 200000000 = 200ms)
  LEAK_MAX_BUFFERS     (default 0; set 1-2 for ultra-low latency)

Stay-live / leaky queue tuning (RTMP sink-side, post-mux):
  SINK_LEAK_TIME_NS     (default 200000000 = 200ms)
  SINK_LEAK_MAX_BUFFERS (default 0; set 1-2 for ultra-low latency)
EOF
}

DRY_RUN=0
MODE="va"

DEBUG_SAVE=0
DEBUG_SAVE_TIER=""
DEBUG_SAVE_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1; shift
      ;;
    va|vaapi|mpp)
      MODE="$1"; shift
      ;;
    --debug-save)
      DEBUG_SAVE=1
      DEBUG_SAVE_TIER="${2:-}"
      DEBUG_SAVE_PATH="${3:-}"
      if [[ -z "$DEBUG_SAVE_TIER" || -z "$DEBUG_SAVE_PATH" ]]; then
        echo "ERROR: --debug-save requires <tier> <path>" >&2
        usage
        exit 2
      fi
      shift 3
      ;;
    -h|--help)
      usage; exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing command: $1" >&2; exit 1; }
}
need_cmd gst-launch-1.0
need_cmd gst-inspect-1.0

need_elem() {
  local e="$1"
  gst-inspect-1.0 "$e" >/dev/null 2>&1 || { echo "ERROR: missing GStreamer element: $e" >&2; exit 1; }
}

# ---- Common config (override via env) ----
VIDEO_DEV="${VIDEO_DEV:-/dev/video1}"           # mpp default
VIDEO_DEV_VA="${VIDEO_DEV_VA:-/dev/video0}"     # va default
ALSA_DEV="${ALSA_DEV:-hw:CARD=Device,DEV=0}"

# Audio caps lock (NEW)
AUDIO_RATE="${AUDIO_RATE:-48000}"
AUDIO_CHANNELS="${AUDIO_CHANNELS:-2}"

VIDEO_W="${VIDEO_W:-1920}"
VIDEO_H="${VIDEO_H:-1080}"

CAPTURE_FPS="${CAPTURE_FPS:-60}"
TOP_FPS="${TOP_FPS:-60}"
LOW_FPS="${LOW_FPS:-30}"

RTMP_HOST="${RTMP_HOST:-192.168.0.116}"
RTMP_PORT="${RTMP_PORT:-6968}"
RTMP_APP="${RTMP_APP:-dillflix-live}"
STREAM="${STREAM:-sports0}"

# ---- Stay-live queue defaults (video branches) ----
LEAK_TIME_NS="${LEAK_TIME_NS:-200000000}"     # 200ms default
LEAK_MAX_BUFFERS="${LEAK_MAX_BUFFERS:-0}"     # set 1-2 for ultra-low latency
LEAK_MAX_BYTES=0

# ---- Stay-live queue defaults (post-mux -> rtmpsink) ----
SINK_LEAK_TIME_NS="${SINK_LEAK_TIME_NS:-200000000}"     # 200ms default
SINK_LEAK_MAX_BUFFERS="${SINK_LEAK_MAX_BUFFERS:-0}"     # set 1-2 for ultra-low latency

rtmp_url() {
  local tier="$1"
  printf 'rtmp://%s:%s/%s/%s_tier%s' "$RTMP_HOST" "$RTMP_PORT" "$RTMP_APP" "$STREAM" "$tier"
}

# Build pipeline as an argv array (safer than eval/strings)
PIPELINE=()
add() { PIPELINE+=("$@"); }

add_video_leaky_queue() {
  add queue \
    "leaky=downstream" \
    "max-size-time=${LEAK_TIME_NS}" \
    "max-size-buffers=${LEAK_MAX_BUFFERS}" \
    "max-size-bytes=${LEAK_MAX_BYTES}"
}

# Leaky queue used right before RTMP sinks (post-mux)
add_sink_leaky_queue() {
  add queue \
    "leaky=downstream" \
    "max-size-time=${SINK_LEAK_TIME_NS}" \
    "max-size-buffers=${SINK_LEAK_MAX_BUFFERS}" \
    "max-size-bytes=0"
}

add_queue() { add queue; }

# The opt-in runner controls a single overlay without restarting this pipeline.
# Keep this next to runner.py, overlay_model.py, and renderer.py.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OVERLAY_ENABLED=0
if [[ -n "${DILLFLIX_OVERLAY_URL:-}" ]]; then
  if [[ -z "${DILLFLIX_OVERLAY_DEVICE:-}" ]]; then
    echo "ERROR: DILLFLIX_OVERLAY_DEVICE is required when DILLFLIX_OVERLAY_URL is set" >&2
    exit 2
  fi
  OVERLAY_ENABLED=1
fi

add_overlay() {
  if [[ "$OVERLAY_ENABLED" -eq 1 ]]; then
    need_elem gdkpixbufoverlay
    add '!' gdkpixbufoverlay name=dillflixOverlay alpha=0
  fi
}

print_or_run() {
  if [[ "$OVERLAY_ENABLED" -eq 1 ]]; then
    local overlay_width="$VIDEO_W" overlay_height="$VIDEO_H"
    if [[ "$MODE" == "mpp" ]]; then
      overlay_width=1920
      overlay_height=1080
    fi
    local -a overlay_command=(python3 "$SCRIPT_DIR/runner.py"
      --width "$overlay_width" --height "$overlay_height" -- "${PIPELINE[@]}")
    if [[ "$DRY_RUN" -eq 1 ]]; then
      printf '%q ' "${overlay_command[@]}"
      printf '\n'
      return 0
    fi
    need_cmd python3
    exec "${overlay_command[@]}"
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf 'gst-launch-1.0 -e '
    printf '%q ' "${PIPELINE[@]}"
    printf '\n'
    return 0
  fi
  gst-launch-1.0 -e "${PIPELINE[@]}"
}

# ---------------- Tier tables ----------------
TIERS=(0 1 2 3 4 5)

# Audio bitrates (bps) per tier
AUDIO_BR=(192000 192000 164000 128000 96000 64000)

# Video ladder (kbps for VA encoders; bps for MPP encoders)
VIDEO_W_LAD=(1920 1920 1280 1280 640 320)
VIDEO_H_LAD=(1080 1080 720  720  480 240)

VIDEO_BR_KBPS=(12000 8000 4000 2000 1000 300)
VIDEO_PROFILE=(high high high high high main)

# Per-tier output FPS: tier0=TOP_FPS, tiers1-5=LOW_FPS
VIDEO_FPS_LAD=("${TOP_FPS}" "${LOW_FPS}" "${LOW_FPS}" "${LOW_FPS}" "${LOW_FPS}" "${LOW_FPS}")

# Throughput help on tiny tiers
VIDEO_CABAC=(true true true true true false)
VIDEO_DCT8X8=(true true true true true false)

# ---------------- Outputs (one flvmux -> one RTMP sink each) ----------------
add_outputs_common() {
  need_elem flvmux
  need_elem rtmpsink

  if [[ "$DEBUG_SAVE" -eq 1 ]]; then
    need_elem tee
    need_elem filesink
    mkdir -p "$(dirname "$DEBUG_SAVE_PATH")"
  fi

  for t in "${TIERS[@]}"; do
    add flvmux "name=flvMux${t}" streamable=true

    if [[ "$DEBUG_SAVE" -eq 1 && "$t" == "$DEBUG_SAVE_TIER" ]]; then
      # Tee muxed FLV: one branch to RTMP, one branch to disk.
      add '!' tee "name=flvOutTee${t}"

      # RTMP branch: leaky queue right before sink (stay-live)
      add "flvOutTee${t}." '!'
      add_sink_leaky_queue
      add '!' rtmpsink "location=$(rtmp_url "$t")" sync=false

      # File branch: regular queue (disk writer is not used for timing)
      add "flvOutTee${t}." '!' queue '!' filesink "location=${DEBUG_SAVE_PATH}" sync=false
    else
      # Normal: leaky queue right before sink (stay-live)
      add '!'
      add_sink_leaky_queue
      add '!' rtmpsink "location=$(rtmp_url "$t")" sync=false
    fi
  done
}

# ---------------- Audio branches (share 192k for tiers 0/1) ----------------
add_audio_branches_common() {
  need_elem alsasrc
  need_elem audioconvert
  need_elem audioresample
  need_elem audiorate          # NEW
  need_elem voaacenc
  need_elem aacparse

  add alsasrc "device=${ALSA_DEV}" do-timestamp=true
  # NEW: lock caps and perfect the audio stream with audiorate
  add '!' audioconvert
  add '!' audioresample
  add '!' "audio/x-raw,rate=${AUDIO_RATE},channels=${AUDIO_CHANNELS}"
  add '!' audiorate
  add '!' tee name=audioTee

  # shared 192k tee for tiers 0 & 1
  add audioTee. '!' queue '!' voaacenc "bitrate=${AUDIO_BR[0]}" '!' aacparse '!' tee name=aac192Tee
  for t in 0 1; do
    add aac192Tee. '!' queue '!' "flvMux${t}."
  done

  # per-tier encode for remaining
  for t in 2 3 4 5; do
    add audioTee. '!' queue '!' voaacenc "bitrate=${AUDIO_BR[t]}" '!' aacparse '!' queue '!' "flvMux${t}."
  done
}

# ---------------- VA encoder selection & safe property helpers ----------------
choose_va_encoder() {
  local requested="${VA_ENCODER:-auto}"
  if [[ "$requested" != "auto" ]]; then
    need_elem "$requested"
    echo "$requested"
    return
  fi
  if gst-inspect-1.0 vah264lpenc >/dev/null 2>&1; then
    echo "vah264lpenc"
    return
  fi
  need_elem vah264enc
  echo "vah264enc"
}

vaenc_supports_token() {
  local enc="$1" token="$2"
  gst-inspect-1.0 "$enc" 2>/dev/null | grep -qiE "(^|[^a-z0-9_])${token}([^a-z0-9_]|$)"
}

pick_rate_control() {
  local enc="$1"
  if vaenc_supports_token "$enc" "cbr"; then echo "cbr"; return; fi
  if vaenc_supports_token "$enc" "vbr"; then echo "vbr"; return; fi
  if vaenc_supports_token "$enc" "icq"; then echo "icq"; return; fi
  if vaenc_supports_token "$enc" "qvbr"; then echo "qvbr"; return; fi
  echo ""
}

# ---------------- VA video branches ----------------
add_va_encode_branch() {
  local tee_name="$1" t="$2" kbps="$3" profile="$4" cabac="$5" dct8x8="$6" fps="$7" enc="$8" rc="$9"
  local use_videorate="${10:-0}"   # NEW: allow tier0 to use videorate too

  add "${tee_name}." '!'
  add_video_leaky_queue

  # Ensure we stay on VA memory + NV12 at the input of the encoder
  add '!' vapostproc

  # NEW: run videorate on tier0 so all tiers are timestamp-regularized by videorate
  if [[ "$use_videorate" -eq 1 ]]; then
    add '!' videorate
  fi

  add '!' "video/x-raw(memory:VAMemory),format=NV12,width=${VIDEO_W_LAD[t]},height=${VIDEO_H_LAD[t]},framerate=${fps}/1"

  add '!' "$enc" \
      "bitrate=${kbps}" \
      "key-int-max=90" \
      "b-frames=0" \
      "cabac=${cabac}" \
      "dct8x8=${dct8x8}" \
      "target-usage=${VA_TARGET_USAGE:-7}"
  if [[ -n "$rc" ]]; then
    add "rate-control=${rc}"
  fi

  add '!' h264parse config-interval=-1
  add '!' "video/x-h264,stream-format=avc,alignment=au,profile=${profile}"
  add '!' queue
  add '!' "flvMux${t}."
}

add_va_video_branches() {
  need_elem v4l2src
  need_elem vapostproc
  need_elem h264parse
  need_elem videorate

  local ENC
  ENC="$(choose_va_encoder)"
  local RC
  RC="$(pick_rate_control "$ENC")"

  # Capture is effectively 60fps on this device even when asking for 30.
  # So we capture at CAPTURE_FPS, convert/upload once to VA memory at CAPTURE_FPS, then split:
  #   - Tier0 from 60fps tee (TOP_FPS=60)  [NOW ALSO THROUGH videorate]
  #   - Tiers1-5 from 30fps tee derived via videorate drop-only=true (LOW_FPS=30)
  add v4l2src "device=${VIDEO_DEV_VA}" do-timestamp=true
  add '!' "video/x-raw,format=YUY2,width=${VIDEO_W},height=${VIDEO_H},framerate=${CAPTURE_FPS}/1"
  add '!' queue
  add_overlay
  add '!' vapostproc
  add '!' "video/x-raw(memory:VAMemory),format=NV12,width=${VIDEO_W},height=${VIDEO_H},framerate=${CAPTURE_FPS}/1"
  add '!' tee name=video60Tee

  # Tier0: 1080p60  (NEW: videorate inserted via flag=1)
  add_va_encode_branch "video60Tee" 0 "${VIDEO_BR_KBPS[0]}" "${VIDEO_PROFILE[0]}" "${VIDEO_CABAC[0]}" "${VIDEO_DCT8X8[0]}" "${VIDEO_FPS_LAD[0]}" "$ENC" "$RC" 1

  # Build a 30fps tee for tiers 1-5 by dropping frames and fixing timestamps.
  add video60Tee. '!'
  add_video_leaky_queue
  add '!' videorate drop-only=true
  add '!' "video/x-raw(memory:VAMemory),framerate=${LOW_FPS}/1"
  add '!' tee name=video30Tee

  # Tier1: 1080p30
  add_va_encode_branch "video30Tee" 1 "${VIDEO_BR_KBPS[1]}" "${VIDEO_PROFILE[1]}" "${VIDEO_CABAC[1]}" "${VIDEO_DCT8X8[1]}" "${VIDEO_FPS_LAD[1]}" "$ENC" "$RC"

  # Create 720p@30 tee for tiers 2/3
  add video30Tee. '!'
  add_video_leaky_queue
  add '!' vapostproc
  add '!' "video/x-raw(memory:VAMemory),format=NV12,width=1280,height=720,framerate=${LOW_FPS}/1"
  add '!' tee name=video720Tee

  add_va_encode_branch "video720Tee" 2 "${VIDEO_BR_KBPS[2]}" "${VIDEO_PROFILE[2]}" "${VIDEO_CABAC[2]}" "${VIDEO_DCT8X8[2]}" "${VIDEO_FPS_LAD[2]}" "$ENC" "$RC"
  add_va_encode_branch "video720Tee" 3 "${VIDEO_BR_KBPS[3]}" "${VIDEO_PROFILE[3]}" "${VIDEO_CABAC[3]}" "${VIDEO_DCT8X8[3]}" "${VIDEO_FPS_LAD[3]}" "$ENC" "$RC"

  # Tier4/5: from 30fps tee, scaled
  add_va_encode_branch "video30Tee" 4 "${VIDEO_BR_KBPS[4]}" "${VIDEO_PROFILE[4]}" "${VIDEO_CABAC[4]}" "${VIDEO_DCT8X8[4]}" "${VIDEO_FPS_LAD[4]}" "$ENC" "$RC"
  add_va_encode_branch "video30Tee" 5 "${VIDEO_BR_KBPS[5]}" "${VIDEO_PROFILE[5]}" "${VIDEO_CABAC[5]}" "${VIDEO_DCT8X8[5]}" "${VIDEO_FPS_LAD[5]}" "$ENC" "$RC"
}

# ---------------- Video branches (MPP) ----------------
add_mpp_video_branches() {
  need_elem v4l2src
  need_elem videoconvert
  need_elem videorate       # NEW
  need_elem mpph264enc
  need_elem h264parse

  local MPP_BPS=(12000000 8000000 4000000 2000000 1000000 300000)
  local MPP_BPS_MAX=(13000000 8800000 4400000 2200000 1100000 330000)
  local MPP_QP=(16 18 18 20 20 20)
  local MPP_PROF=(high high high high high main)

  add v4l2src "device=${VIDEO_DEV}" do-timestamp=true
  add '!' videoconvert
  # NEW: regularize timestamps / framerate before tee so all tiers share it
  add '!' videorate
  add '!' "video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1"
  add_overlay
  add '!' tee name=videoTee

  for t in "${TIERS[@]}"; do
    add videoTee. '!'
    add_video_leaky_queue
    add '!' mpph264enc \
      "width=${VIDEO_W_LAD[t]}" "height=${VIDEO_H_LAD[t]}" \
      "bps=${MPP_BPS[t]}" "bps-min=${MPP_BPS[t]}" "bps-max=${MPP_BPS_MAX[t]}" \
      rc-mode=cbr "qp-init=${MPP_QP[t]}" "profile=${MPP_PROF[t]}" level=41 \
      gop=90 min-force-key-unit-interval=3333333333 header-mode=each-idr \
      max-reenc=0 qos=false sei-mode=disable zero-copy-pkt=true
    add '!' h264parse '!' queue '!' "flvMux${t}."
  done
}

# ========================= Build pipeline per mode =========================
case "$MODE" in
  va|vaapi)
    add_outputs_common
    add_audio_branches_common
    add_va_video_branches
    ;;

  mpp)
    add_outputs_common
    add_audio_branches_common
    add_mpp_video_branches
    ;;

  *)
    echo "ERROR: unknown mode: $MODE (use va|vaapi|mpp)" >&2
    exit 2
    ;;
esac

print_or_run
