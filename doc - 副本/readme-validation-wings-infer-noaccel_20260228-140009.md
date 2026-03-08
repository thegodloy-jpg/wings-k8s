# README Validation (wings-infer only, no wings-accel)

Date: 2026-02-28
Project: F:\zhanghui\wings-k8s\infer-control-sidecar-main\infer-control-sidecar-main
Validation mode: `wings-infer only` (initContainer `wings-accel` removed)
Remote: `7.6.52.148`
Workspace: `/home/zhanghui/infer-control-sidecar-verify`
Cluster: `k3s-verify`
Namespace: `wings-verify`

## 1) Scope and method

- Referenced doc: `F:\zhanghui\wings-k8s\doc\infer-control-sidecar-startup-test-steps_20260228-135035.md`
- Goal: validate README startup workflow under isolated environment without touching production containers.

## 2) What was verified

1. Deployment can be created in isolated namespace.
2. `wings-infer` control container starts and writes `/shared-volume/start_command.sh`.
3. `vllm-engine` container reads startup command and attempts to start engine.
4. Health and API probe endpoints are reachable from `wings-infer` container.

## 3) Actual runtime status

- `Deployment/wings-infer`: `Available`
- Pod: `2/2 Running`
- Service: `ClusterIP` on `18000`

Generated startup command (from shared volume):

```bash
python3 -m vllm.entrypoints.openai.api_server --host 0.0.0.0 --port 17000 ...
```

## 4) Key findings against README

### Finding A: README API port examples are outdated

README shows `http://<EXTERNAL-IP>:9000/...` examples, but current runtime and service config are `18000` (proxy) and `19000` (health).

Evidence:
- `k8s/service.yaml` => `port: 18000`, `targetPort: 18000`
- Runtime probe valid on `18000/19000`, not `9000`

### Finding B: README deployment path includes accel by default, but this validation intentionally excludes it

Current `k8s/deployment.yaml` uses `wings-accel` initContainer and accel volume flow.
This validation explicitly removed accel chain per requirement.

### Finding C: Main blocker is vLLM device detection in this topology

`vllm-engine` logs show:

```text
RuntimeError: Failed to infer device type
```

Resulting API behavior:
- `GET /v1/models` => `502 Backend unavailable`
- `POST /v1/chat/completions` => `502 backend connect error`

This means startup chain is validated, but end-to-end inference readiness is not achieved in current no-accel setup.

## 5) Conclusion

README is partially valid for architecture and command flow, but at least two updates are required for current code/runtime alignment:

1. Replace `9000` API examples with `18000` (and note health on `19000`).
2. Clarify accel-dependent vs no-accel deployment variants and corresponding prerequisites.

## 6) Related evidence file

- vLLM engine K8s error log (no accel): see `vllm-engine-k8s-error-log-noaccel_*.md` in same directory.
