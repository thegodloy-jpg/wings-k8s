#!/bin/bash
SGPID=$(docker top k3s-verify-agent-zhanghui 2>&1 | grep sglang.launch | head -1 | awk '{print $2}')
echo "WORKER sglang PID: $SGPID"
nsenter -t $SGPID -n python3 /tmp/test_conn.py 2>&1
echo "---RESULT---"
