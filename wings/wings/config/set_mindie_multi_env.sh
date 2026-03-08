source /opt/mindie_env/bin/activate
source /usr/local/Ascend/ascend-toolkit/set_env.sh
source /usr/local/Ascend/nnal/atb/set_env.sh
source /usr/local/Ascend/mindie/set_env.sh
source /opt/atb-models/set_env.sh

export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export NPU_MEMORY_FRACTION=0.97
export ATB_WORKSPACE_MEM_ALLOC_ALG_TYPE=3
export ATB_WORKSPACE_MEM_ALLOC_GLOBAL=1
export OMP_NUM_THREADS=10
export HCCL_DETERMINISTIC=false
export HCCL_OP_EXPANSION_MODE="AIV"
export ATB_LLM_HCCL_ENABLE=1
export ATB_LLM_COMM_BACKEND="hccl"
export INF_NAN_MODE_ENABLE=0
export TASK_QUEUE_ENABLE=2
export CPU_AFFINITY_CONF=1
unset ASCEND_LAUNCH_BLOCKING
export ATB_LAYER_INTERNAL_TENSOR_REUSE=1
export ATB_OPERATION_EXECUTE_ASYNC=1
export ATB_CONVERT_NCHW_TO_ND=1
export ATB_CONTEXT_WORKSPACE_SIZE=0
export ATB_LAUNCH_KERNEL_WITH_TILING=1
export ATB_LLM_ENABLE_AUTO_TRANSPOSE=0

export ATB_LLM_LCOC_ENABLE=0

export HCCL_CONNECT_TIMEOUT=7200
export HCCL_EXEC_TIMEOUT=0
export MINDIE_LOG_TO_STDOUT=1
