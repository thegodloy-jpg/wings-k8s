import re, os

def extract_env_vars(root, exclude_dirs=None):
    pattern = re.compile(
        r'os\.(?:getenv|environ\.get|environ\.setdefault)\(\s*["\x27]([A-Z_][A-Z0-9_]*)["\x27]'
        r'|os\.environ\[\s*["\x27]([A-Z_][A-Z0-9_]*)["\x27]\s*\]'
        r'|os\.environ\.pop\(\s*["\x27]([A-Z_][A-Z0-9_]*)["\x27]'
    )

    # Also capture default values for os.getenv
    default_pattern = re.compile(
        r'os\.getenv\(\s*["\x27]([A-Z_][A-Z0-9_]*)["\x27]\s*,\s*["\x27]([^"\x27]*)["\x27]\s*\)'
    )

    vars_found = {}
    defaults = {}

    for dirpath, dirnames, filenames in os.walk(root):
        if exclude_dirs:
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            fp = os.path.join(dirpath, fn)
            with open(fp, encoding='utf-8') as f:
                for i, line in enumerate(f, 1):
                    for m in pattern.finditer(line):
                        name = m.group(1) or m.group(2) or m.group(3)
                        if name:
                            if name not in vars_found:
                                vars_found[name] = []
                            vars_found[name].append(f'{fn}:{i}')

                    for m in default_pattern.finditer(line):
                        name = m.group(1)
                        default = m.group(2)
                        if name not in defaults:
                            defaults[name] = {}
                        defaults[name][f'{fn}:{i}'] = default

    return vars_found, defaults

a_root = r'd:\project\wings-k8s-v1\wings-k8s\wings-k8s\wings\wings'
b_root = r'd:\project\wings-k8s-v1\wings-k8s\wings-k8s\infer-control-sidecar-unified\backend\app'

a_vars, a_defaults = extract_env_vars(a_root, exclude_dirs=['servers', '__pycache__'])
b_vars, b_defaults = extract_env_vars(b_root, exclude_dirs=['__pycache__'])

a_names = set(a_vars.keys())
b_names = set(b_vars.keys())

# A有B无
a_only = sorted(a_names - b_names)
# B有A无
b_only = sorted(b_names - a_names)
# 共有
common = sorted(a_names & b_names)

# 默认值差异
default_diffs = []
for name in common:
    a_defs = set(a_defaults.get(name, {}).values())
    b_defs = set(b_defaults.get(name, {}).values())
    if a_defs and b_defs and a_defs != b_defs:
        default_diffs.append((name, a_defs, b_defs))

print(f'A total unique vars: {len(a_names)}')
print(f'B total unique vars: {len(b_names)}')
print(f'Common: {len(common)}')
print(f'A only: {len(a_only)}')
print(f'B only: {len(b_only)}')
print(f'Default value diffs: {len(default_diffs)}')
print()

print('=== A ONLY (wings有, unified无) ===')
for n in a_only:
    locs = ', '.join(a_vars[n][:3])
    defs = list(a_defaults.get(n, {}).values())
    def_str = f' (default: {defs[0]})' if defs else ''
    print(f'  {n}{def_str}  [{locs}]')

print()
print('=== B ONLY (unified有, wings无) ===')
for n in b_only:
    locs = ', '.join(b_vars[n][:3])
    defs = list(b_defaults.get(n, {}).values())
    def_str = f' (default: {defs[0]})' if defs else ''
    print(f'  {n}{def_str}  [{locs}]')

print()
print('=== DEFAULT VALUE DIFFERENCES ===')
for name, a_defs, b_defs in sorted(default_diffs):
    print(f'  {name}:')
    print(f'    A defaults: {a_defs}')
    print(f'    B defaults: {b_defs}')

print()
print('=== COMMON VARS (同名一致) ===')
for n in common:
    if n not in [x[0] for x in default_diffs]:
        print(f'  {n}')
