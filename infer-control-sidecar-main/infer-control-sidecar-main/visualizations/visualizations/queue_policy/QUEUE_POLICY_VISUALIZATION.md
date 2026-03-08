# Queue Policy Visualization (vLLM V1)

## 1. FCFS waiting queue

```mermaid
flowchart LR
  A[Request A arrives t1] --> Q1
  B[Request B arrives t2] --> Q1
  C[Request C arrives t3] --> Q1

  subgraph Q1[FCFS Queue: deque]
    direction LR
    H[Head] --> QA[A] --> QB[B] --> QC[C] --> T[Tail]
  end

  Q1 --> P1[Pop from head: A]
  P1 --> P2[Next: B]
  P2 --> P3[Next: C]
```

Mechanism:
- enqueue: `append`
- dequeue: `popleft`
- data structure: `deque`

Code:
- `vllm/v1/core/sched/request_queue.py` (`FCFSRequestQueue`)

## 2. Priority waiting queue

```mermaid
flowchart TB
  A[A: priority=5, arrival=10] --> H
  B[B: priority=1, arrival=12] --> H
  C[C: priority=1, arrival=11] --> H

  H[Priority Queue: heapq min-heap] --> O1[Pop #1: C]
  O1 --> O2[Pop #2: B]
  O2 --> O3[Pop #3: A]

  R[Ordering Rule] --> R1[1. lower priority first]
  R --> R2[2. earlier arrival_time first]
  R --> R3[3. smaller request_id tie-break]
```

Mechanism:
- enqueue: `heappush`
- dequeue: `heappop`
- comparator: `Request.__lt__`

Code:
- `vllm/v1/core/sched/request_queue.py` (`PriorityRequestQueue`)
- `vllm/v1/request.py` (`__lt__`)

## 3. Scheduler main loop (shared by both policies)

```mermaid
flowchart TD
  S[Start schedule step] --> R[Schedule RUNNING requests first]
  R --> W[Schedule WAITING requests]
  W --> A[Try allocate_slots for request]
  A -->|success| M[Move request to RUNNING / continue execution]
  A -->|fail: KV insufficient| P[Preempt one RUNNING request]
  P --> Q[Put preempted request back to WAITING]
  Q --> A
  M --> B[Update token budget and stats]
  B --> E[Emit scheduler output]
```

Code:
- `vllm/v1/core/sched/scheduler.py` (`schedule`)
- `vllm/v1/core/kv_cache_manager.py` (`allocate_slots`)

## 4. Preemption behavior difference

```mermaid
flowchart LR
  K[KV allocation fails] --> D{Policy}
  D -->|priority| P1[Preempt lowest-priority RUNNING request]
  D -->|fcfs| P2[Preempt tail RUNNING request \n self.running.pop()]
  P1 --> W1[Request status -> PREEMPTED]
  P2 --> W1
  W1 --> W2[Free KV blocks]
  W2 --> W3[Prepend back to WAITING queue]
```

Code:
- `vllm/v1/core/sched/scheduler.py` (`_preempt_request` and branch in `schedule`)

## 5. Sequence view (single scheduling step)

```mermaid
sequenceDiagram
  participant Scheduler
  participant WaitingQueue
  participant KVManager
  participant RunningList

  Scheduler->>RunningList: Iterate RUNNING first
  Scheduler->>WaitingQueue: Peek next waiting request
  Scheduler->>KVManager: allocate_slots(request)
  alt allocation success
    KVManager-->>Scheduler: new blocks
    Scheduler->>RunningList: append / keep running
  else allocation fail
    KVManager-->>Scheduler: None
    Scheduler->>RunningList: select victim to preempt
    Scheduler->>KVManager: free(victim)
    Scheduler->>WaitingQueue: prepend(victim)
  end
  Scheduler-->>Scheduler: emit SchedulerOutput
```
