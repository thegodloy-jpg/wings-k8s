#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Ascend CANN + ATB environment initialization for vllm-ascend engine
# Used by: engines/vllm_adapter.py -> build_start_script (vllm_ascend branch)
# Compatible with: quay.io/ascend/vllm-ascend:v0.14.0rc1
# ---------------------------------------------------------------------------

source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
