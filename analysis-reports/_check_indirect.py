import re, os

for label, root in [('A', r'd:\project\wings-k8s-v1\wings-k8s\wings-k8s\wings\wings'), ('B', r'd:\project\wings-k8s-v1\wings-k8s\wings-k8s\infer-control-sidecar-unified\backend\app')]:
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn == 'noise_filter.py':
                fp = os.path.join(dirpath, fn)
                with open(fp, encoding='utf-8') as f:
                    content = f.read()
                pat = re.compile(r'"([A-Z][A-Z0-9_]+)"\s*:')
                matches = pat.findall(content)
                print(f'{label} noise_filter dict keys: {matches}')

    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn == 'speaker_logging.py':
                fp = os.path.join(dirpath, fn)
                with open(fp, encoding='utf-8') as f:
                    content = f.read()
                pat = re.compile(r'_env_(?:bool|int)\(\s*"([A-Z][A-Z0-9_]+)"')
                matches = pat.findall(content)
                print(f'{label} speaker_logging indirect: {matches}')

    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn in ('wings_proxy.py', 'wings_proxy_old.py', 'main.py'):
                fp = os.path.join(dirpath, fn)
                with open(fp, encoding='utf-8') as f:
                    content = f.read()
                pat = re.compile(r'os\.environ\.setdefault\(\s*"([A-Z][A-Z0-9_]+)"')
                matches = pat.findall(content)
                if matches:
                    print(f'{label} {fn} setdefault: {matches}')

# Also check for MINDIE_WORK_DIR and MINDIE_CONFIG_PATH patterns in B
for dirpath, _, filenames in os.walk(r'd:\project\wings-k8s-v1\wings-k8s\wings-k8s\infer-control-sidecar-unified\backend\app'):
    for fn in filenames:
        if fn == 'mindie_adapter.py':
            fp = os.path.join(dirpath, fn)
            with open(fp, encoding='utf-8') as f:
                for i, line in enumerate(f, 1):
                    if 'MINDIE_WORK_DIR' in line or 'MINDIE_CONFIG_PATH' in line:
                        print(f'B mindie_adapter.py:{i}: {line.strip()}')
