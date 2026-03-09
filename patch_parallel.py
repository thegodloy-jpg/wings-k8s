import os

path = "/usr/local/lib/python3.12/dist-packages/vllm/config/parallel.py"
with open(path, "r") as f:
    content = f.read()

old = "                self._data_parallel_master_port_list = get_open_ports_list(5)"
new = ("                _fp = os.environ.get('VLLM_DP_FIXED_PORTS', '')\n"
       "                self._data_parallel_master_port_list = list(map(int, _fp.split(','))) if _fp else get_open_ports_list(5)")

if old in content:
    content = content.replace(old, new, 1)
    with open(path, "w") as f:
        f.write(content)
    print("PATCHED OK")
elif "VLLM_DP_FIXED_PORTS" in content:
    print("ALREADY PATCHED")
else:
    print("NOT FOUND - old string not matched")
    # Show context
    idx = content.find("get_open_ports_list")
    if idx >= 0:
        print("Context:", repr(content[idx-50:idx+100]))
