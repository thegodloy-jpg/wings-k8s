#!/usr/bin/env bash
set -u
set -o pipefail

# -----------------------------------------------------------------------------
# Wings-Infer 一键全流程脚本：
# - 清理历史残留
# - 构建镜像
# - 部署到 Kubernetes
# - 分步骤验证启动/探针/转发/Service
#
# 运行模式：
#   full   （默认）：clean + build + deploy + verify
#   deploy           build + deploy
#   verify           仅验证当前运行环境
#   clean            仅清理历史资源
# -----------------------------------------------------------------------------

ACTION="${1:-full}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${PROJECT_ROOT:-$SCRIPT_DIR}"

NS="${NS:-default}"
DEPLOY="${DEPLOY:-wings-infer}"
SERVICE_NAME="${SERVICE_NAME:-wings-infer-service}"
APP_LABEL="${APP_LABEL:-app=wings-infer}"
WINGS_CONTAINER="${WINGS_CONTAINER:-wings-infer}"
ENGINE_CONTAINER="${ENGINE_CONTAINER:-vllm-engine}"
MODEL_NAME="${MODEL_NAME:-DeepSeek-R1-Distill-Qwen-1.5B}"

DEPLOYMENT_FILE="${DEPLOYMENT_FILE:-$PROJECT_ROOT/k8s/deployment.yaml}"
SERVICE_FILE="${SERVICE_FILE:-$PROJECT_ROOT/k8s/service.yaml}"

IMAGE_REPO="${IMAGE_REPO:-wings-infer}"
IMAGE_TAG="${IMAGE_TAG:-$(date +%Y%m%d-%H%M%S)}"
IMAGE="${IMAGE:-${IMAGE_REPO}:${IMAGE_TAG}}"
IMAGE_BUILDER="${IMAGE_BUILDER:-auto}"   # auto|nerdctl|docker|none
CLEAN_IMAGES="${CLEAN_IMAGES:-1}"

RUN_CHAT_TEST="${RUN_CHAT_TEST:-1}"
CURL_TIMEOUT="${CURL_TIMEOUT:-3}"
ENGINE_WAIT_SECS="${ENGINE_WAIT_SECS:-420}"
ENGINE_WAIT_STEP="${ENGINE_WAIT_STEP:-5}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-10m}"
CLEAN_WAIT_SECS="${CLEAN_WAIT_SECS:-120}"

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
STEP_COUNT=0
LAST_BODY=""
POD=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $*"; }
pass() { PASS_COUNT=$((PASS_COUNT + 1)); echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); echo -e "${RED}[FAIL]${NC} $*"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); echo -e "${YELLOW}[WARN]${NC} $*"; }
step() { STEP_COUNT=$((STEP_COUNT + 1)); echo; echo -e "${BLUE}========== 第 ${STEP_COUNT} 步：$* ==========${NC}"; }

usage() {
  cat <<EOF
用法：
  bash verify_e2e.sh [full|deploy|verify|clean]

模式说明：
  full    清理历史资源、构建镜像、部署并完整验证（默认）
  deploy  构建镜像并部署（不做验证）
  verify  仅验证当前已部署环境
  clean   仅清理历史残留资源

常用环境变量：
  NS=default
  DEPLOY=wings-infer
  SERVICE_NAME=wings-infer-service
  APP_LABEL=app=wings-infer
  DEPLOYMENT_FILE=./k8s/deployment.yaml
  SERVICE_FILE=./k8s/service.yaml
  IMAGE_REPO=wings-infer
  IMAGE_TAG=<自动时间戳>
  IMAGE=<repo:tag>                 # 优先于 IMAGE_REPO/IMAGE_TAG
  IMAGE_BUILDER=auto|nerdctl|docker|none
  RUN_CHAT_TEST=1|0
  ENGINE_WAIT_SECS=420
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo -e "${RED}[FATAL]${NC} 缺少命令：$cmd"
    exit 2
  fi
}

run_or_die() {
  info "执行：$*"
  "$@"
  local rc=$?
  if [ $rc -ne 0 ]; then
    echo -e "${RED}[FATAL]${NC} 命令执行失败（rc=$rc）：$*"
    exit 3
  fi
}

remove_images_by_repo() {
  local tool="$1"
  local ns="$2"
  local repo="$3"
  local ids=""

  if [ "$tool" = "docker" ]; then
    ids="$(docker images --format '{{.Repository}} {{.ID}}' 2>/dev/null | awk -v r="$repo" '$1==r{print $2}' | sort -u)"
    if [ -z "$ids" ]; then
      info "docker 无需清理镜像（repo=${repo}）"
      return 0
    fi
    for id in $ids; do
      run_cmd docker rmi -f "$id" >/dev/null 2>&1 || true
    done
    pass "docker 已清理镜像（repo=${repo}）"
    return 0
  fi

  ids="$(nerdctl -n "$ns" images --format '{{.Repository}} {{.ID}}' 2>/dev/null | awk -v r="$repo" '$1==r{print $2}' | sort -u)"
  if [ -z "$ids" ]; then
    info "nerdctl($ns) 无需清理镜像（repo=${repo}）"
    return 0
  fi
  for id in $ids; do
    run_cmd nerdctl -n "$ns" rmi -f "$id" >/dev/null 2>&1 || true
  done
  pass "nerdctl($ns) 已清理镜像（repo=${repo}）"
}

clean_local_images() {
  if [ "$CLEAN_IMAGES" != "1" ]; then
    warn "CLEAN_IMAGES=0，跳过本地镜像清理"
    return
  fi
  step "清理本地镜像"
  if command -v docker >/dev/null 2>&1; then
    remove_images_by_repo docker "" "$IMAGE_REPO"
  fi
  if command -v nerdctl >/dev/null 2>&1; then
    remove_images_by_repo nerdctl "k8s.io" "$IMAGE_REPO"
    remove_images_by_repo nerdctl "default" "$IMAGE_REPO"
  fi
}

run_cmd() {
  info "执行：$*"
  "$@"
  return $?
}

is_containerd_runtime() {
  local runtime
  runtime="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null || true)"
  echo "$runtime" | grep -qi "containerd"
}

get_base_image_from_dockerfile() {
  local dockerfile="${PROJECT_ROOT}/Dockerfile"
  if [ ! -f "$dockerfile" ]; then
    return 1
  fi
  awk '/^[[:space:]]*FROM[[:space:]]+/ {print $2; exit}' "$dockerfile"
}

image_exists_in_nerdctl() {
  local image="$1"
  nerdctl -n k8s.io image inspect "$image" >/dev/null 2>&1
}

image_exists_in_docker() {
  local image="$1"
  docker image inspect "$image" >/dev/null 2>&1
}

ensure_base_image_for_nerdctl() {
  local base_image="$1"
  if [ -z "$base_image" ]; then
    warn "未能从 Dockerfile 解析 FROM 基础镜像，跳过基础镜像预热"
    return 0
  fi

  if image_exists_in_nerdctl "$base_image"; then
    pass "containerd(k8s.io) 已存在基础镜像：$base_image"
    return 0
  fi

  if command -v docker >/dev/null 2>&1 && image_exists_in_docker "$base_image"; then
    info "containerd 缺少基础镜像，尝试从 docker 导入：$base_image"
    if docker save "$base_image" | nerdctl -n k8s.io load >/dev/null 2>&1; then
      pass "基础镜像导入成功：$base_image"
      return 0
    fi
    warn "基础镜像导入失败：$base_image"
    return 1
  fi

  warn "本地 docker 也不存在基础镜像：$base_image，离线环境可能构建失败"
  return 1
}

import_built_image_to_nerdctl() {
  local image="$1"
  if ! command -v docker >/dev/null 2>&1 || ! command -v nerdctl >/dev/null 2>&1; then
    return 1
  fi
  info "尝试把 docker 镜像导入 containerd(k8s.io)：$image"
  docker save "$image" | nerdctl -n k8s.io load >/dev/null 2>&1
}

detect_builder() {
  if [ "$IMAGE_BUILDER" != "auto" ]; then
    echo "$IMAGE_BUILDER"
    return
  fi

  local runtime
  runtime="$(kubectl get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null || true)"
  if echo "$runtime" | grep -qi "containerd" && command -v nerdctl >/dev/null 2>&1; then
    echo "nerdctl"
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    echo "docker"
    return
  fi
  if command -v nerdctl >/dev/null 2>&1; then
    echo "nerdctl"
    return
  fi
  echo "none"
}

refresh_pod() {
  POD="$(kubectl get pod -n "$NS" -l "$APP_LABEL" --sort-by=.metadata.creationTimestamp -o name 2>/dev/null | tail -n 1 | cut -d/ -f2)"
}

wait_no_pods() {
  local end=$((SECONDS + CLEAN_WAIT_SECS))
  while [ $SECONDS -lt $end ]; do
    local cnt
    cnt="$(kubectl get pod -n "$NS" -l "$APP_LABEL" --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    if [ "${cnt:-0}" = "0" ]; then
      pass "标签 ${APP_LABEL} 下无残留 Pod"
      return 0
    fi
    sleep 2
  done
  warn "${CLEAN_WAIT_SECS}s 内仍存在残留 Pod"
  kubectl get pod -n "$NS" -l "$APP_LABEL" || true
  return 1
}

clean_stack() {
  step "清理历史资源"
  run_or_die kubectl delete deployment -n "$NS" "$DEPLOY" --ignore-not-found=true
  run_or_die kubectl delete service -n "$NS" "$SERVICE_NAME" --ignore-not-found=true
  run_or_die kubectl delete rs -n "$NS" -l "$APP_LABEL" --ignore-not-found=true
  run_or_die kubectl delete pod -n "$NS" -l "$APP_LABEL" --ignore-not-found=true --wait=false
  wait_no_pods || true
  clean_local_images
}

build_image() {
  step "构建镜像"
  local builder
  local base_image
  local build_ok=0
  builder="$(detect_builder)"
  base_image="$(get_base_image_from_dockerfile || true)"
  info "镜像构建器：${builder}"
  info "目标镜像：${IMAGE}"

  case "$builder" in
    nerdctl)
      require_cmd nerdctl
      ensure_base_image_for_nerdctl "$base_image" || true
      if run_cmd nerdctl -n k8s.io build --pull=false -t "$IMAGE" "$PROJECT_ROOT"; then
        build_ok=1
      else
        warn "nerdctl 构建失败，尝试回退到 docker 构建并导入 containerd"
        if command -v docker >/dev/null 2>&1; then
          if run_cmd docker build -t "$IMAGE" "$PROJECT_ROOT"; then
            if is_containerd_runtime && command -v nerdctl >/dev/null 2>&1; then
              if import_built_image_to_nerdctl "$IMAGE"; then
                pass "docker 构建结果已导入 containerd：$IMAGE"
                build_ok=1
              else
                fail "docker 构建成功，但导入 containerd 失败：$IMAGE"
              fi
            else
              build_ok=1
            fi
          fi
        fi
      fi
      ;;
    docker)
      require_cmd docker
      if run_cmd docker build -t "$IMAGE" "$PROJECT_ROOT"; then
        build_ok=1
        if is_containerd_runtime && command -v nerdctl >/dev/null 2>&1; then
          if import_built_image_to_nerdctl "$IMAGE"; then
            pass "docker 镜像已导入 containerd：$IMAGE"
          else
            warn "docker 构建成功，但导入 containerd 失败；若 Pod 拉不到镜像，请改用 IMAGE_BUILDER=nerdctl"
          fi
        fi
      fi
      ;;
    none)
      warn "跳过构建（IMAGE_BUILDER=none）"
      if command -v nerdctl >/dev/null 2>&1 && image_exists_in_nerdctl "$IMAGE"; then
        pass "复用 containerd 现有镜像：$IMAGE"
        build_ok=1
      elif command -v docker >/dev/null 2>&1 && image_exists_in_docker "$IMAGE"; then
        if is_containerd_runtime && command -v nerdctl >/dev/null 2>&1; then
          if import_built_image_to_nerdctl "$IMAGE"; then
            pass "已将现有 docker 镜像导入 containerd：$IMAGE"
            build_ok=1
          else
            fail "现有 docker 镜像导入 containerd 失败：$IMAGE"
          fi
        else
          pass "复用 docker 现有镜像：$IMAGE"
          build_ok=1
        fi
      else
        fail "跳过构建失败：本地不存在镜像 $IMAGE"
      fi
      ;;
    *)
      echo -e "${RED}[FATAL]${NC} 不支持的 IMAGE_BUILDER=${builder}"
      exit 2
      ;;
  esac

  if [ "$build_ok" -ne 1 ]; then
    echo -e "${RED}[FATAL]${NC} 镜像构建阶段失败，请检查上面的构建日志"
    exit 3
  fi
  pass "镜像可用：${IMAGE}"

  if command -v nerdctl >/dev/null 2>&1; then
    if nerdctl -n k8s.io image inspect "$IMAGE" >/dev/null 2>&1; then
      pass "containerd(k8s.io) 镜像可见：$IMAGE"
    else
      warn "containerd(k8s.io) 未找到镜像：$IMAGE"
    fi
  fi
  if command -v docker >/dev/null 2>&1; then
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
      info "docker 本地镜像可见：$IMAGE"
    else
      info "docker 本地无该镜像（若集群使用 containerd 属正常）"
    fi
  fi
}

deploy_stack() {
  step "部署资源"
  [ -f "$DEPLOYMENT_FILE" ] || { echo -e "${RED}[FATAL]${NC} 缺少文件 $DEPLOYMENT_FILE"; exit 2; }
  [ -f "$SERVICE_FILE" ] || { echo -e "${RED}[FATAL]${NC} 缺少文件 $SERVICE_FILE"; exit 2; }

  run_or_die kubectl apply -n "$NS" -f "$SERVICE_FILE"
  run_or_die kubectl apply -n "$NS" -f "$DEPLOYMENT_FILE"

  # 强制使用本次构建镜像 tag，避免 latest 缓存干扰。
  run_or_die kubectl set image -n "$NS" "deployment/$DEPLOY" "${WINGS_CONTAINER}=${IMAGE}"

  if kubectl rollout status -n "$NS" "deployment/$DEPLOY" --timeout="$ROLLOUT_TIMEOUT"; then
    pass "Deployment 滚动完成"
  else
    fail "Deployment 在 ${ROLLOUT_TIMEOUT} 内未完成滚动"
  fi

  refresh_pod
  if [ -n "$POD" ]; then
    pass "当前 Pod：$POD"
  else
    fail "部署后未找到 Pod"
  fi
}

get_jsonpath() {
  local resource_args="$1"
  local jsonpath="$2"
  kubectl get $resource_args -o "jsonpath=${jsonpath}" 2>/dev/null || true
}

pod_exec() {
  local container="$1"
  shift
  kubectl exec -n "$NS" -c "$container" "$POD" -- sh -c "$*" 2>&1
}

http_call() {
  local container="$1"
  local url="$2"
  local timeout="${3:-$CURL_TIMEOUT}"
  pod_exec "$container" "curl -sS -m ${timeout} -w '\n__HTTP_CODE__:%{http_code}\n' '${url}'"
}

extract_code() {
  local raw="$1"
  echo "$raw" | sed -n 's/^__HTTP_CODE__://p' | tail -n 1 | tr -d '\r'
}

extract_body() {
  local raw="$1"
  echo "$raw" | sed '/^__HTTP_CODE__:/d'
}

check_http_200() {
  local title="$1"
  local container="$2"
  local url="$3"
  local timeout="${4:-$CURL_TIMEOUT}"

  local raw
  raw="$(http_call "$container" "$url" "$timeout")"
  local rc=$?
  if [ $rc -ne 0 ]; then
    fail "${title}：请求异常：${raw}"
    LAST_BODY=""
    return 1
  fi

  local code
  code="$(extract_code "$raw")"
  LAST_BODY="$(extract_body "$raw")"

  if [ "$code" = "200" ]; then
    pass "${title}：200 OK"
    return 0
  fi

  fail "${title}：期望 200，实际 ${code}"
  echo "$LAST_BODY" | sed -n '1,4p'
  return 1
}

wait_engine_health() {
  local url="$1"
  local end=$((SECONDS + ENGINE_WAIT_SECS))
  while [ $SECONDS -lt $end ]; do
    local raw
    raw="$(http_call "$WINGS_CONTAINER" "$url" "$CURL_TIMEOUT")"
    if [ $? -eq 0 ]; then
      local code
      code="$(extract_code "$raw")"
      if [ "$code" = "200" ]; then
        pass "引擎健康检查就绪：${url}"
        return 0
      fi
    fi
    sleep "$ENGINE_WAIT_STEP"
  done
  fail "引擎健康检查超时（${ENGINE_WAIT_SECS}s）：${url}"
  return 1
}

verify_stack() {
  step "全链路验证"

  refresh_pod
  if [ -z "${POD}" ]; then
    fail "命名空间 ${NS} 下，标签 ${APP_LABEL} 未找到 Pod"
    return 1
  fi

  info "命名空间：${NS}"
  info "Deployment：${DEPLOY}"
  info "Service：${SERVICE_NAME}"
  info "Pod：${POD}"

  local proxy_port health_port engine_port
  local readiness_port liveness_port
  local service_port service_target_port

  proxy_port="$(get_jsonpath "deploy -n ${NS} ${DEPLOY}" "{.spec.template.spec.containers[?(@.name=='wings-infer')].env[?(@.name=='PORT')].value}")"
  health_port="$(get_jsonpath "deploy -n ${NS} ${DEPLOY}" "{.spec.template.spec.containers[?(@.name=='wings-infer')].env[?(@.name=='HEALTH_PORT')].value}")"
  engine_port="$(get_jsonpath "deploy -n ${NS} ${DEPLOY}" "{.spec.template.spec.containers[?(@.name=='wings-infer')].env[?(@.name=='ENGINE_PORT')].value}")"
  readiness_port="$(get_jsonpath "deploy -n ${NS} ${DEPLOY}" "{.spec.template.spec.containers[?(@.name=='wings-infer')].readinessProbe.httpGet.port}")"
  liveness_port="$(get_jsonpath "deploy -n ${NS} ${DEPLOY}" "{.spec.template.spec.containers[?(@.name=='wings-infer')].livenessProbe.httpGet.port}")"
  service_port="$(get_jsonpath "svc -n ${NS} ${SERVICE_NAME}" "{.spec.ports[0].port}")"
  service_target_port="$(get_jsonpath "svc -n ${NS} ${SERVICE_NAME}" "{.spec.ports[0].targetPort}")"

  [ -n "$proxy_port" ] || proxy_port="18000"
  [ -n "$health_port" ] || health_port="19000"
  [ -n "$engine_port" ] || engine_port="17000"

  info "端口配置：ENGINE=${engine_port} PROXY=${proxy_port} HEALTH=${health_port}"
  info "探针端口：readiness=${readiness_port:-<unset>} liveness=${liveness_port:-<unset>}"
  info "Service 映射：${service_port:-<unset>} -> ${service_target_port:-<unset>}"

  # 检查容器内代码签名，识别新 launcher 还是旧 main.py 流程。
  local has_core has_launcher_sig has_old_sig
  has_core="$(pod_exec "$WINGS_CONTAINER" "test -d /app/app/core && echo yes || echo no" | tail -n 1)"
  has_launcher_sig="$(pod_exec "$WINGS_CONTAINER" "grep -c 'parse_launch_args' /app/app/main.py || true" | tail -n 1)"
  has_old_sig="$(pod_exec "$WINGS_CONTAINER" "grep -c 'engine_manager.start' /app/app/main.py || true" | tail -n 1)"
  if [ "$has_core" = "yes" ] && [ "${has_launcher_sig}" != "0" ]; then
    pass "代码签名符合 launcher 版本"
  else
    warn "代码签名疑似非 launcher 版本（core=${has_core}, parse_launch_args=${has_launcher_sig}）"
  fi
  if [ "${has_old_sig}" != "0" ]; then
    warn "main.py 中仍检测到旧 engine_manager.start 流程"
  fi

  if [ -n "$readiness_port" ] && [ "$readiness_port" != "$health_port" ]; then
    warn "readinessProbe 端口（${readiness_port}）与 HEALTH_PORT（${health_port}）不一致"
  else
    pass "readinessProbe 端口与 HEALTH_PORT 一致"
  fi
  if [ -n "$liveness_port" ] && [ "$liveness_port" != "$health_port" ]; then
    warn "livenessProbe 端口（${liveness_port}）与 HEALTH_PORT（${health_port}）不一致"
  else
    pass "livenessProbe 端口与 HEALTH_PORT 一致"
  fi

  if [ -n "$service_target_port" ] && [ "$service_target_port" != "$proxy_port" ]; then
    fail "Service targetPort（${service_target_port}）与代理 PORT（${proxy_port}）不一致"
  else
    pass "Service targetPort 与代理 PORT 一致"
  fi

  info "容器 ready/restart 状态："
  kubectl get pod -n "$NS" "$POD" -o jsonpath='{range .status.containerStatuses[*]}{.name}{" ready="}{.ready}{" restarts="}{.restartCount}{"\n"}{end}' || true

  local artifact_info
  artifact_info="$(pod_exec "$WINGS_CONTAINER" "ls -l /shared-volume/start_command.sh 2>/dev/null || true")"
  if echo "$artifact_info" | grep -q "start_command.sh"; then
    pass "共享卷启动产物存在：/shared-volume/start_command.sh"
  else
    fail "共享卷启动产物缺失：/shared-volume/start_command.sh"
  fi

  wait_engine_health "http://127.0.0.1:${engine_port}/health"
  check_http_200 "探针接口检查" "$WINGS_CONTAINER" "http://127.0.0.1:${health_port}/health"
  check_http_200 "代理 /health 检查" "$WINGS_CONTAINER" "http://127.0.0.1:${proxy_port}/health"
  check_http_200 "代理转发 /v1/models" "$WINGS_CONTAINER" "http://127.0.0.1:${proxy_port}/v1/models" 10
  local models_body="$LAST_BODY"

  local ready_eps notready_eps
  ready_eps="$(kubectl get endpoints -n "$NS" "$SERVICE_NAME" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null | wc -w | tr -d ' ')"
  notready_eps="$(kubectl get endpoints -n "$NS" "$SERVICE_NAME" -o jsonpath='{.subsets[*].notReadyAddresses[*].ip}' 2>/dev/null | wc -w | tr -d ' ')"
  if [ "${ready_eps:-0}" -gt 0 ]; then
    pass "Service endpoints 就绪地址数量：${ready_eps}"
  else
    fail "Service endpoints 无就绪地址（notReady=${notready_eps:-0}）"
  fi

  if [ -n "$service_port" ]; then
    check_http_200 "Service DNS /health 检查" "$WINGS_CONTAINER" "http://${SERVICE_NAME}.${NS}.svc.cluster.local:${service_port}/health" 10
  else
    fail "Service port 为空，无法验证 Service DNS"
  fi

  if [ "$RUN_CHAT_TEST" = "1" ]; then
    local model_from_models payload raw code body
    model_from_models="$(echo "$models_body" | tr -d '\n' | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    [ -n "$model_from_models" ] || model_from_models="$MODEL_NAME"
    payload="{\"model\":\"${model_from_models}\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":8}"
    raw="$(pod_exec "$WINGS_CONTAINER" "curl -sS -m 90 -H 'Content-Type: application/json' -d '${payload}' -w '\n__HTTP_CODE__:%{http_code}\n' 'http://127.0.0.1:${proxy_port}/v1/chat/completions'")"
    if [ $? -ne 0 ]; then
      fail "代理 chat 烟测请求执行失败"
    else
      code="$(extract_code "$raw")"
      body="$(extract_body "$raw")"
      if [ "$code" = "200" ]; then
        pass "代理 chat 烟测通过（model=${model_from_models}）"
      else
        fail "代理 chat 烟测失败，HTTP ${code}"
        echo "$body" | sed -n '1,6p'
      fi
    fi
  else
    warn "RUN_CHAT_TEST=0，跳过 chat 烟测"
  fi

  echo
  info "最近 Pod 事件："
  kubectl describe pod -n "$NS" "$POD" | sed -n '/^Events:/,$p' | tail -n 25 || true
}

print_summary() {
  echo
  info "汇总：PASS=${PASS_COUNT} WARN=${WARN_COUNT} FAIL=${FAIL_COUNT}"
  if [ "$FAIL_COUNT" -gt 0 ]; then
    echo -e "${RED}[RESULT] FAIL${NC}"
    return 1
  fi
  echo -e "${GREEN}[RESULT] PASS${NC}"
  return 0
}

preflight() {
  step "前置检查"
  require_cmd kubectl
  if ! kubectl cluster-info >/dev/null 2>&1; then
    echo -e "${RED}[FATAL]${NC} 无法连接 Kubernetes 集群"
    exit 2
  fi
  [ -f "$DEPLOYMENT_FILE" ] || { echo -e "${RED}[FATAL]${NC} 缺少文件 ${DEPLOYMENT_FILE}"; exit 2; }
  [ -f "$SERVICE_FILE" ] || { echo -e "${RED}[FATAL]${NC} 缺少文件 ${SERVICE_FILE}"; exit 2; }
  pass "前置检查通过"
}

main() {
  case "$ACTION" in
    full|deploy|verify|clean) ;;
    help|--help|-h) usage; exit 0 ;;
    *) echo -e "${RED}[FATAL]${NC} 未知模式：$ACTION"; usage; exit 2 ;;
  esac

  preflight

  case "$ACTION" in
    clean)
      clean_stack
      ;;
    deploy)
      build_image
      deploy_stack
      ;;
    verify)
      verify_stack
      ;;
    full)
      clean_stack
      build_image
      deploy_stack
      verify_stack
      ;;
  esac

  print_summary
}

main "$@"
