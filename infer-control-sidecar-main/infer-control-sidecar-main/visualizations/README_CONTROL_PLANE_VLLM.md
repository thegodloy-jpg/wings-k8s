# vLLM 生产控制面可视化文档索引

本索引汇总了“如何把外部治理能力和 vLLM 引擎组合”的控制面机制文档。

## 文档列表

1. `docs/visualizations/ADMISSION_TOKEN_BUDGET_WITH_VLLM.md`
2. `docs/visualizations/PREFIX_KV_AWARE_ROUTING_WITH_VLLM.md`
3. `docs/visualizations/PREFILL_DECODE_DISAGG_WITH_VLLM.md`
4. `docs/visualizations/KV_TIERING_OFFLOAD_WITH_VLLM.md`
5. `docs/visualizations/LLM_METRICS_AUTOSCALING_WITH_VLLM.md`
6. `docs/visualizations/SLO_PRIORITY_CLASSES_WITH_VLLM.md`
7. `docs/visualizations/WINGS_TMATRIX_BOUNDARY_WITH_VLLM.md`
8. `docs/visualizations/WINGS_TMATRIX_INTERFACE_CONTRACT_DRAFT.md`

## 阅读路径图

```mermaid
flowchart LR
  A[Admission] --> B[SLO 优先级]
  B --> C[Prefix/KV 路由]
  C --> D[Autoscaling]
  D --> E[KV Offload]
  E --> F[P/D 解耦]
  F --> G[职责边界]
  G --> H[接口契约]
```

## 范围说明

- 每个文件只讲一个角度。
- 重点是实现机制与 vLLM 对接点。
- 默认不绑定特定厂商控制面实现。
