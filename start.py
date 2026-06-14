#!/usr/bin/env python3
"""Hermes Collab Engine launcher — reads API config from ~/.hermes/ first, falls back to ~/.claude/."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HERMES_DIR = Path.home() / '.hermes'
CLAUDE_DIR = Path.home() / '.claude'

VERSION = 'v5.0'
GITHUB_URL = 'https://github.com/lpc0387/hermes-collab-engine'
TAGLINE_ZH = '多 Agent 协同引擎 · Leader 拆解 · Worker 并行 · 面板可视化'
TAGLINE_EN = 'Multi-agent collab engine · Leader plans · Workers run in parallel · Live dashboard'

# Agent config files this launcher reads/writes. Must share the same parent
# directory as the project root (path-consistency invariant).
AGENT_CONFIG_DIRS = [HERMES_DIR, CLAUDE_DIR]
AGENT_CONFIG_FILES = [
    HERMES_DIR / '.env',
    HERMES_DIR / 'config.yaml',
    HERMES_DIR / 'auth.json',
    CLAUDE_DIR / 'settings.json',
]
RUNTIME_CONFIG_PATH = ROOT / '.runtime-config.json'


def _supports_color() -> bool:
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    return hasattr(__import__('sys').stdout, 'isatty') and __import__('sys').stdout.isatty()


def print_banner() -> None:
    """Render the launcher banner: project name, tagline, GitHub link, version."""
    use_color = _supports_color()
    C = {
        'reset': '\033[0m' if use_color else '',
        'cyan':  '\033[38;5;51m'  if use_color else '',
        'blue':  '\033[38;5;75m'  if use_color else '',
        'mag':   '\033[38;5;213m' if use_color else '',
        'gray':  '\033[38;5;245m' if use_color else '',
        'green': '\033[38;5;120m' if use_color else '',
        'bold':  '\033[1m'         if use_color else '',
        'dim':   '\033[2m'         if use_color else '',
    }

    # Compact ASCII logo — readable at 80 cols, Hermes caduceus motif on the side.
    logo_lines = [
        " _   _                                ____      _ _       _     ",
        "| | | | ___ _ __ _ __ ___   ___  ___ / ___|___ | | | __ _| |__  ",
        "| |_| |/ _ \\ '__| '_ ` _ \\ / _ \\/ __| |   / _ \\| | |/ _` | '_ \\ ",
        "|  _  |  __/ |  | | | | | |  __/\\__ \\ |__| (_) | | | (_| | |_) |",
        "|_| |_|\\___|_|  |_| |_| |_|\\___||___/\\____\\___/|_|_|\\__,_|_.__/ ",
        "                          E N G I N E                            ",
    ]

    width = max(len(line) for line in logo_lines)
    bar = '─' * width

    print()
    print(f"{C['gray']}{bar}{C['reset']}")
    for line in logo_lines:
        print(f"{C['cyan']}{C['bold']}{line}{C['reset']}")
    print(f"{C['gray']}{bar}{C['reset']}")
    print(f"  {C['mag']}{C['bold']}Hermes Collab Engine{C['reset']}  "
          f"{C['green']}{VERSION}{C['reset']}  "
          f"{C['dim']}{C['gray']}— 协同引擎启动器 / Launcher{C['reset']}")
    print(f"  {C['blue']}▸{C['reset']} {C['gray']}{TAGLINE_ZH}{C['reset']}")
    print(f"  {C['blue']}▸{C['reset']} {C['gray']}{TAGLINE_EN}{C['reset']}")
    print(f"  {C['blue']}⌬{C['reset']} {C['cyan']}GitHub:{C['reset']} "
          f"{C['bold']}{GITHUB_URL}{C['reset']}")
    print(f"{C['gray']}{bar}{C['reset']}")
    print()


DEFAULT_MODELS = [
    'kimi-k2.6', 'glm-5.1', 'deepseek-v4-pro', 'deepseek-v4-flash',
    'doubao-seed-2.0-lite', 'doubao-seed-2.0-pro', 'doubao-seed-2.0-code',
    'minimax-m2.7', 'minimax-m3',
    'mimo-v2.5', 'mimo-v2.5-pro[1M]', 'mimo-v2-pro[1M]',
]


def load_json_lenient(path: Path):
    text = path.read_text(encoding='utf-8')
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return json.loads(text)


def unique(items):
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


# ── Hermes config sources ──────────────────────────────────────────────

def read_hermes_env() -> dict | None:
    """Read ~/.hermes/.env for ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL."""
    env_path = HERMES_DIR / '.env'
    if not env_path.exists():
        return None
    kv = {}
    for line in env_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            k, v = line.split('=', 1)
            kv[k.strip()] = v.strip()
    token = kv.get('ANTHROPIC_API_KEY') or kv.get('ANTHROPIC_AUTH_TOKEN')
    base_url = kv.get('ANTHROPIC_BASE_URL')
    if not token or not base_url:
        return None
    return {'source': 'Hermes .env', 'source_path': str(env_path),
            'base_url': base_url, 'token': token, 'models': None,
            'default_leader': None, 'default_worker': None}


def read_hermes_auth() -> dict | None:
    """Read ~/.hermes/auth.json credential pool for anthropic credentials."""
    auth_path = HERMES_DIR / 'auth.json'
    if not auth_path.exists():
        return None
    try:
        data = load_json_lenient(auth_path)
    except Exception:
        return None
    pool = data.get('credential_pool', {})
    anthropic = pool.get('anthropic', [])
    if not anthropic:
        return None
    cred = anthropic[0]  # highest priority
    base_url = cred.get('base_url')
    if not base_url:
        return None
    # The actual secret is not stored in auth.json, need the env var
    token = os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')
    if not token:
        return None
    return {'source': 'Hermes auth.json', 'source_path': str(auth_path),
            'base_url': base_url, 'token': token, 'models': None,
            'default_leader': None, 'default_worker': None}


def read_hermes_config_yaml() -> dict | None:
    """Read ~/.hermes/config.yaml for model.base_url and model.default."""
    config_path = HERMES_DIR / 'config.yaml'
    if not config_path.exists():
        return None
    try:
        import yaml
        data = yaml.safe_load(config_path.read_text(encoding='utf-8'))
    except Exception:
        # Fallback: simple regex parse for base_url
        text = config_path.read_text(encoding='utf-8')
        m = re.search(r'base_url:\s*["\']?(\S+?)["\']?\s*$', text, re.M)
        base_url = m.group(1) if m else None
        dm = re.search(r'default:\s*(\S+)', text)
        default_model = dm.group(1) if dm else None
        if not base_url:
            return None
        token = os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')
        if not token:
            return None
        return {'source': 'Hermes config.yaml', 'source_path': str(config_path),
                'base_url': base_url, 'token': token,
                'models': [default_model] if default_model else None,
                'default_leader': default_model, 'default_worker': None}
    base_url = (data.get('model') or {}).get('base_url')
    default_model = (data.get('model') or {}).get('default')
    if not base_url:
        return None
    token = os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN')
    if not token:
        return None
    return {'source': 'Hermes config.yaml', 'source_path': str(config_path),
            'base_url': base_url, 'token': token,
            'models': [default_model] if default_model else None,
            'default_leader': default_model, 'default_worker': None}


def collect_hermes_configs():
    """Try Hermes config sources in priority order."""
    sources = []
    for reader in (read_hermes_env, read_hermes_config_yaml, read_hermes_auth):
        result = reader()
        if result:
            sources.append(result)
    return sources


# ── Claude config sources (fallback) ───────────────────────────────────

def collect_claude_profiles():
    profiles = []
    settings = CLAUDE_DIR / 'settings.json'
    if settings.exists():
        try:
            profiles.append({'name': 'Claude Code 当前配置', 'path': str(settings),
                             'data': load_json_lenient(settings)})
        except Exception:
            pass
    profiles_dir = CLAUDE_DIR / 'profiles'
    if profiles_dir.exists():
        for p in sorted(profiles_dir.glob('*.json')):
            try:
                profiles.append({'name': p.stem, 'path': str(p),
                                 'data': load_json_lenient(p)})
            except Exception:
                pass
    return profiles


def models_from_claude(profile):
    data = profile['data']
    env = data.get('env', {})
    models = []
    for key in ['ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                'ANTHROPIC_DEFAULT_HAIKU_MODEL']:
        models.append(env.get(key))
    models.extend(data.get('availableModels') or [])
    return unique(models)


def get_config_from_claude():
    profiles = collect_claude_profiles()
    if not profiles:
        return None
    labels = []
    for p in profiles:
        env = p['data'].get('env', {})
        labels.append(f"{p['name']} | {env.get('ANTHROPIC_BASE_URL','未设置 BaseURL')} | 模型数 {len(models_from_claude(p))}")
    selected = choose('选择 Claude 配置来源', labels)
    profile = profiles[labels.index(selected)]
    env = profile['data'].get('env', {})
    token = env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY')
    base_url = env.get('ANTHROPIC_BASE_URL')
    models = models_from_claude(profile)
    if not token or not base_url:
        print('该配置缺少 BaseURL 或 API Key。')
        return None
    return {'source': profile['name'], 'source_path': profile['path'],
            'base_url': base_url, 'token': token,
            'models': models or DEFAULT_MODELS,
            'default_leader': env.get('ANTHROPIC_DEFAULT_OPUS_MODEL'),
            'default_worker': env.get('ANTHROPIC_DEFAULT_SONNET_MODEL')}


# ── UI helpers ─────────────────────────────────────────────────────────

def choose(label, items, default=1):
    print(f'\n{label}')
    for i, item in enumerate(items, 1):
        print(f'  {i}. {item}')
    while True:
        raw = input(f'请选择编号 [默认 {default}]: ').strip()
        try:
            idx = default if not raw else int(raw)
            if 1 <= idx <= len(items):
                return items[idx - 1]
        except ValueError:
            pass
        print('输入无效，请重新选择。')


def prompt(text, default=''):
    suffix = f' [默认 {default}]' if default else ''
    raw = input(f'{text}{suffix}: ').strip()
    return raw or default


# ── Path consistency & previous-runtime loading ────────────────────────

def enforce_path_consistency() -> None:
    """Verify agent config dirs share the project's parent directory.

    The launcher reads/writes config from ~/.hermes/ and ~/.claude/. We require
    those directories (and the project root) to live under a common parent so
    deployments stay self-contained — e.g. all of /root/hermes-collab-engine,
    /root/.hermes, /root/.claude under /root. If a dir exists but lives
    elsewhere, abort instead of silently reading the "wrong" config.
    """
    project_parent = ROOT.parent.resolve()

    print('检查 agent 配置路径一致性...')
    print(f'  项目根目录: {ROOT}')
    print(f'  期望共同父路径: {project_parent}')
    print('  将读取以下 agent 配置文件（如存在）：')
    for f in AGENT_CONFIG_FILES:
        print(f'    - {f}')

    errors = []
    for d in AGENT_CONFIG_DIRS:
        if not d.exists():
            # Missing dirs are fine — caller decides whether they were needed.
            continue
        try:
            real = d.resolve()
        except OSError as e:
            errors.append(f'无法解析 {d}: {e}')
            continue
        # Walk up parents looking for the shared parent.
        if project_parent not in real.parents and real != project_parent:
            errors.append(
                f'{d} 解析为 {real}，与项目父目录 {project_parent} '
                f'不在同一路径树下'
            )

    if errors:
        print()
        print('✗ 路径一致性检查失败（agent 配置目录与项目不在同一父路径下）：')
        for e in errors:
            print(f'  - {e}')
        print()
        print('请将 agent 配置目录放到与本项目相同的父目录下，或调整项目位置后重试。')
        raise SystemExit(2)

    print('  ✓ 路径一致性 OK')
    print()


def load_previous_runtime() -> dict:
    """Load the most recent .runtime-config.json so empty answers can keep prior values."""
    if not RUNTIME_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(RUNTIME_CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception as e:
        print(f'⚠ 无法读取上次的 runtime 配置（{RUNTIME_CONFIG_PATH}）：{e}')
        print('  将视为首次启动。')
        return {}


def _typing_animation(label: str, dots: int = 4, interval: float = 0.25) -> None:
    """Print '<label>。' progressively to convey 'filling in...' feedback."""
    sys = __import__('sys')
    sys.stdout.write(label)
    sys.stdout.flush()
    for _ in range(dots):
        time.sleep(interval)
        sys.stdout.write('。')
        sys.stdout.flush()
    sys.stdout.write('\n')
    sys.stdout.flush()


def _mask(token: str) -> str:
    if not token:
        return '(空)'
    if len(token) <= 8:
        return '*' * len(token)
    return f'{token[:4]}...{token[-4:]}'


def ask_agent_config(role_label: str, prev: dict) -> dict:
    """Prompt for one agent's base_url / api_key / model. Empty -> keep prev value.

    `prev` is the previously persisted dict for this role (may be empty). If a
    field has no previous value AND the user leaves it blank, abort.
    """
    print(f'── 配置 {role_label} ──')
    if prev:
        print(f'  上次值：base_url={prev.get("base_url") or "(无)"}'
              f'  api_key={_mask(prev.get("api_key", ""))}'
              f'  model={prev.get("model") or "(无)"}')
        print('  留空则沿用上次配置；任意一项首次启动且留空将报错退出。')
    else:
        print('  首次配置，三项均为必填。')

    fields = [
        ('base_url', f'{role_label} BaseURL'),
        ('api_key',  f'{role_label} API Key / Auth Token'),
        ('model',    f'{role_label} 模型名称'),
    ]
    out = {}
    for key, label in fields:
        prev_val = prev.get(key, '') if prev else ''
        hint = _mask(prev_val) if key == 'api_key' and prev_val else (prev_val or '')
        suffix = f' [留空保留上次值: {hint}]' if hint else ''
        raw = input(f'  {label}{suffix}: ').strip()
        if not raw:
            if not prev_val:
                print(f'  ✗ {label} 为空且无历史值，无法启动。')
                raise SystemExit(2)
            print(f'  · {label} 留空 → 沿用上次值')
            out[key] = prev_val
        else:
            out[key] = raw

    print()
    _typing_animation(f'  正在填入 {role_label} 配置')
    print(f'  ✓ {role_label}: {out["base_url"]}  |  '
          f'key={_mask(out["api_key"])}  |  model={out["model"]}')
    print()
    return out


def choose_interaction_mode():
    choice = choose(
        '选择操作方式',
        [
            'Web 面板操作（使用浏览器中的任务输入窗口，推荐）',
            'Hermes 命令行操作（进入终端交互）',
        ],
        1,
    )
    return 'cli' if 'Hermes 命令行' in choice else 'web'


# ── Config selection ───────────────────────────────────────────────────

def get_config_from_hermes():
    """Auto-detect from ~/.hermes/ — merge .env + config.yaml for best result."""
    env_cfg = read_hermes_env()
    yaml_cfg = read_hermes_config_yaml()
    auth_cfg = read_hermes_auth()

    # Collect all unique sources found
    found = [c for c in (env_cfg, yaml_cfg, auth_cfg) if c]
    if not found:
        return None

    # Merge: prefer .env for secrets, config.yaml for models
    base_url = None
    token = None
    models = None
    default_leader = None
    default_worker = None
    source_parts = []

    for cfg in found:
        if cfg.get('base_url') and not base_url:
            base_url = cfg['base_url']
        if cfg.get('token') and not token:
            token = cfg['token']
        if cfg.get('models') and not models:
            models = cfg['models']
        if cfg.get('default_leader') and not default_leader:
            default_leader = cfg['default_leader']
        if cfg.get('default_worker') and not default_worker:
            default_worker = cfg['default_worker']
        source_parts.append(cfg['source'])

    if not base_url or not token:
        return None

    return {
        'source': ' + '.join(source_parts),
        'source_path': found[0].get('source_path', ''),
        'base_url': base_url,
        'token': token,
        'models': models or DEFAULT_MODELS,
        'default_leader': default_leader,
        'default_worker': default_worker,
    }


def get_config_manual():
    print('\n手动填写 API 配置')
    base_url = prompt('BaseURL，例如 https://api.example.com/anthropic')
    token = prompt('API Key / Auth Token')
    raw_models = prompt('可用模型名称，多个用英文逗号分隔', ','.join(DEFAULT_MODELS[:3]))
    models = unique([x.strip() for x in raw_models.split(',')])
    if not base_url or not token or not models:
        raise SystemExit('手动配置不完整。')
    return {
        'source': '手动输入', 'source_path': '',
        'base_url': base_url, 'token': token,
        'models': models,
        'default_leader': models[0],
        'default_worker': models[min(1, len(models) - 1)],
    }


# ── Main ───────────────────────────────────────────────────────────────

# ── Agent config registry ──────────────────────────────────────────────
# Each agent registers how its config files should be synced.
# To add a new agent, append an entry to AGENT_CONFIG_REGISTRY.
# Format: (name, config_path_builder, sync_function)
#   config_path_builder(leader, worker) -> Path | None
#   sync_function(path, leader, worker) -> None (writes config)

def _sync_hermes_env(path: Path, leader: dict, worker: dict) -> None:
    """Sync ~/.hermes/.env — key=value format."""
    import re as _re
    if path.exists():
        lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    else:
        lines = ['# Hermes Agent secrets\n']
    updates = {'ANTHROPIC_API_KEY': leader['api_key'], 'ANTHROPIC_BASE_URL': leader['base_url']}
    for key, val in updates.items():
        pattern = _re.compile(rf'^{key}=.*$')
        found = False
        for i, line in enumerate(lines):
            if pattern.match(line.strip()):
                lines[i] = f'{key}={val}\n'
                found = True
                break
        if not found:
            lines.append(f'{key}={val}\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(lines), encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f'  ✓ 已同步 → {path}')


def _sync_claude_settings(path: Path, leader: dict, worker: dict) -> None:
    """Sync ~/.claude/settings.json — JSON format with env block."""
    if path.exists():
        try:
            settings = load_json_lenient(path)
        except Exception:
            settings = {}
    else:
        settings = {}
    env_block = settings.setdefault('env', {})
    env_block['ANTHROPIC_AUTH_TOKEN'] = leader['api_key']
    env_block['ANTHROPIC_API_KEY'] = leader['api_key']
    env_block['ANTHROPIC_BASE_URL'] = leader['base_url']
    if leader.get('model'):
        env_block['ANTHROPIC_DEFAULT_OPUS_MODEL'] = leader['model']
        env_block['ANTHROPIC_DEFAULT_OPUS_MODEL_NAME'] = leader['model']
    if worker.get('model'):
        env_block['ANTHROPIC_DEFAULT_SONNET_MODEL'] = worker['model']
        env_block['ANTHROPIC_DEFAULT_SONNET_MODEL_NAME'] = worker['model']
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f'  ✓ 已同步 → {path}')


# Agent config registry: (name, path_builder, sync_fn)
# path_builder receives (leader, worker) and returns the config file Path or None.
# To add a new agent, append a tuple here.
AGENT_CONFIG_REGISTRY = [
    ('hermes', lambda l, w: HERMES_DIR / '.env', _sync_hermes_env),
    ('claude', lambda l, w: CLAUDE_DIR / 'settings.json', _sync_claude_settings),
]


def sync_agent_configs(leader: dict, worker: dict) -> None:
    """Write leader config back to all registered agent config files.

    Iterates AGENT_CONFIG_REGISTRY — each agent defines its own config path
    and sync function. To add a new agent, append to AGENT_CONFIG_REGISTRY.
    """
    for name, path_builder, sync_fn in AGENT_CONFIG_REGISTRY:
        config_path = path_builder(leader, worker)
        if config_path is None:
            continue
        try:
            sync_fn(config_path, leader, worker)
        except Exception as e:
            print(f'  ⚠ {name} 配置同步失败: {e}')


def stop_existing_server():
    subprocess.run(['pkill', '-f', 'src.hermes_collab_engine.cli server'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    print_banner()

    # 1. Path-consistency invariant — agent config dirs must live alongside the
    #    project. Fails fast and exits if the user's environment is misaligned.
    enforce_path_consistency()

    # 2. Load the previous runtime so each field can fall back to its prior value.
    prev_runtime = load_previous_runtime()
    prev_leader = prev_runtime.get('leader', {})
    prev_worker = prev_runtime.get('worker', {})

    # Backfill from older flat-format runtime files (pre-v4.6) so first-run
    # after upgrade still gets useful "previous values".
    if not prev_leader and prev_runtime.get('base_url'):
        prev_leader = {
            'base_url': prev_runtime.get('base_url', ''),
            'api_key':  prev_runtime.get('api_key', ''),
            'model':    prev_runtime.get('leader_model', ''),
        }
    if not prev_worker and prev_runtime.get('base_url'):
        prev_worker = {
            'base_url': prev_runtime.get('base_url', ''),
            'api_key':  prev_runtime.get('api_key', ''),
            'model':    prev_runtime.get('worker_model', ''),
        }

    # 3. Two rounds of prompts — Leader first, then Worker.
    leader = ask_agent_config('Leader Agent（规划/聚合大脑）', prev_leader)
    worker = ask_agent_config('Worker Agent（执行器大脑）',  prev_worker)

    host = prompt('管理面板监听地址', prev_runtime.get('host') or '0.0.0.0')
    port = prompt('管理面板监听端口', str(prev_runtime.get('port') or '8765'))
    cwd  = prompt('协同任务默认工作目录', prev_runtime.get('cwd') or str(Path.home()))

    runtime = {
        'config_source': 'manual (leader/worker independent)',
        'leader': leader,
        'worker': worker,
        # Legacy mirrors so other tooling reading the old keys keeps working.
        'base_url':     leader['base_url'],
        'leader_model': leader['model'],
        'worker_model': worker['model'],
        'host': host,
        'port': int(port),
        'cwd': cwd,
    }
    RUNTIME_CONFIG_PATH.write_text(
        json.dumps(runtime, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.chmod(RUNTIME_CONFIG_PATH, 0o600)  # contains api_keys
    except OSError:
        pass

    # ── Sync agent config files ────────────────────────────────────────
    # Write leader values back to ~/.hermes/.env and ~/.claude/settings.json
    # so that agent tools (Hermes CLI, Claude Code) pick up the new config
    # without needing opc as an intermediary.
    sync_agent_configs(leader, worker)

    # Build subprocess env. Leader values drive the env vars (Hermes CLI inherits
    # these); worker values are passed via HERMES_COLLAB_WORKER_* and CLI flags so
    # the engine can split leader/worker traffic onto different keys/base_urls.
    run_env = os.environ.copy()
    run_env['ANTHROPIC_AUTH_TOKEN'] = leader['api_key']
    run_env['ANTHROPIC_API_KEY']    = leader['api_key']
    run_env['ANTHROPIC_BASE_URL']   = leader['base_url']
    run_env['ANTHROPIC_MODEL']      = leader['model']
    run_env['HERMES_COLLAB_LEADER_MODEL']    = leader['model']
    run_env['HERMES_COLLAB_LEADER_BASE_URL'] = leader['base_url']
    run_env['HERMES_COLLAB_LEADER_API_KEY']  = leader['api_key']
    run_env['HERMES_COLLAB_WORKER_MODEL']    = worker['model']
    run_env['HERMES_COLLAB_WORKER_BASE_URL'] = worker['base_url']
    run_env['HERMES_COLLAB_WORKER_API_KEY']  = worker['api_key']

    server_cmd = [
        str(ROOT / 'hermes-collab'), 'server', '--host', host, '--port', str(port),
        '--cwd', cwd, '--db', str(ROOT / 'data' / 'collab.sqlite3'),
        '--leader-model', leader['model'], '--worker-model', worker['model'],
    ]

    # Print summary (mask api keys).
    print('启动配置：')
    safe_runtime = json.loads(json.dumps(runtime, ensure_ascii=False))
    for role in ('leader', 'worker'):
        if isinstance(safe_runtime.get(role), dict) and 'api_key' in safe_runtime[role]:
            safe_runtime[role]['api_key'] = _mask(safe_runtime[role]['api_key'])
    print(json.dumps(safe_runtime, ensure_ascii=False, indent=2))

    leader_model = leader['model']  # alias used below

    print('\n正在启动协同引擎管理面板...')
    stop_existing_server()
    log_path = ROOT / 'data' / 'server.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open('a', encoding='utf-8')
    server = subprocess.Popen(server_cmd, env=run_env, cwd=ROOT,
                              stdout=log_file, stderr=subprocess.STDOUT)
    time.sleep(1.5)
    if server.poll() is not None:
        print(f'管理面板启动失败，请查看日志：{log_path}')
        return 1

    display_host = host if host != '0.0.0.0' else '服务器IP'
    print(f'管理面板已启动：http://{display_host}:{port}')
    print(f'服务日志：{log_path}')

    interaction_mode = choose_interaction_mode()
    hermes_cmd = ['hermes', '--provider', 'anthropic', '--model', leader_model]

    try:
        if interaction_mode == 'web':
            print('\n已选择 Web 面板操作。')
            print(f'请在浏览器打开：http://{display_host}:{port}')
            print('你可以直接在面板中的任务输入窗口提交协同任务。')
            print('按 Ctrl-C 退出时，本启动脚本会停止本次启动的管理面板。\n')
            while True:
                time.sleep(60)
        print('\n正在进入 Hermes 命令行...')
        print('退出 Hermes 后，本启动脚本会停止本次启动的管理面板。\n')
        if os.environ.get('OPC_SKIP_HERMES') == '1':
            print('OPC_SKIP_HERMES=1，跳过进入 Hermes（用于测试启动脚本）。')
            while True:
                time.sleep(60)
        subprocess.run(hermes_cmd, env=run_env, cwd=cwd)
    except KeyboardInterrupt:
        pass
    finally:
        print('\n正在停止协同引擎管理面板...')
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        log_file.close()
        print('已退出。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
