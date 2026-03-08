# vLLM Queue Policy Visualization

This folder contains visual documentation for vLLM V1 queue scheduling behavior.

## Files

- `QUEUE_POLICY_VISUALIZATION.md`
  - Explanation of FCFS and Priority queue policy in vLLM.
  - Includes source code anchors in the official repo.
- `queue_policy.mmd`
  - Mermaid diagrams for:
    - FCFS waiting queue behavior
    - Priority waiting queue behavior
    - Scheduler main loop
    - Preemption behavior under KV pressure

## Source Anchors

- Queue policies:
  - `vllm/v1/core/sched/request_queue.py`
- Main scheduling loop:
  - `vllm/v1/core/sched/scheduler.py`
- Priority comparison rule:
  - `vllm/v1/request.py`
