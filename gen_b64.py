import base64

# The patch Python script
patch_code = """import os
path = '/usr/local/lib/python3.12/dist-packages/vllm/config/parallel.py'
with open(path) as f:
    content = f.read()
old = '                self._data_parallel_master_port_list = get_open_ports_list(5)'
new = "                _fp = os.environ.get('VLLM_DP_FIXED_PORTS', '')\\n                self._data_parallel_master_port_list = list(map(int, _fp.split(','))) if _fp else get_open_ports_list(5)"
if old in content:
    with open(path, 'w') as f:
        f.write(content.replace(old, new, 1))
    print('[patch] parallel.py PATCHED OK')
elif 'VLLM_DP_FIXED_PORTS' in content:
    print('[patch] parallel.py already patched')
else:
    print('[patch] WARNING: pattern not found')
"""

b64 = base64.b64encode(patch_code.encode()).decode()
print(b64)
print(f"\nLength: {len(b64)}")
