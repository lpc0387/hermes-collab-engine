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

VERSION = 'v4.5'
GITHUB_URL = 'https://github.com/lpc0387/hermes-collab-engine'
TAGLINE_ZH = '多 Agent 协同引擎 · Leader 拆解 · Worker 并行 · 面板可视化'
TAGLINE_EN = 'Multi-agent collab engine · Leader plans · Workers run in parallel · Live dashboard'


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

def stop_existing_server():
    subprocess.run(['pkill', '-f', 'src.hermes_collab_engine.cli server'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    print_banner()

    # Build config source options dynamically
    hermes_auto = get_config_from_hermes()
    claude_profiles = collect_claude_profiles()
    has_claude = len(claude_profiles) > 0

    options = []
    if hermes_auto:
        options.append(f'自动读取 Hermes 配置（{hermes_auto["source"]}）')
    if has_claude:
        options.append('读取 Claude Code 配置文件')
    options.append('手动填写 BaseURL、API Key 和模型名称')

    mode = choose('API 配置方式', options, 1)

    cfg = None
    if hermes_auto and mode.startswith('自动读取 Hermes'):
        cfg = hermes_auto
        print(f'已从 Hermes 配置加载：{cfg["source"]}')
        print(f'  BaseURL: {cfg["base_url"]}')
        print(f'  模型: {", ".join(cfg["models"][:5])}{"..." if len(cfg["models"]) > 5 else ""}')
    elif has_claude and mode.startswith('读取 Claude'):
        cfg = get_config_from_claude()
    else:
        cfg = get_config_manual()

    if cfg is None:
        print('自动读取失败，切换为手动填写。')
        cfg = get_config_manual()

    models = cfg['models']
    default_leader = models.index(cfg['default_leader']) + 1 if cfg.get('default_leader') in models else 1
    default_worker = models.index(cfg['default_worker']) + 1 if cfg.get('default_worker') in models else min(2, len(models))

    leader_model = choose('选择 Leader Agent（Hermes 命令行 / 规划与聚合大脑）模型', models, default_leader)
    worker_model = choose('Worker Agent（执行器大脑）模型', models, default_worker)

    host = prompt('\n管理面板监听地址', '0.0.0.0')
    port = prompt('管理面板监听端口', '8765')
    cwd = prompt('协同任务默认工作目录', str(Path.home()))

    runtime = {
        'config_source': cfg['source'],
        'config_source_path': cfg['source_path'],
        'base_url': cfg['base_url'],
        'leader_model': leader_model,
        'worker_model': worker_model,
        'host': host,
        'port': int(port),
        'cwd': cwd,
    }
    (ROOT / '.runtime-config.json').write_text(
        json.dumps(runtime, ensure_ascii=False, indent=2), encoding='utf-8')

    run_env = os.environ.copy()
    run_env['ANTHROPIC_AUTH_TOKEN'] = cfg['token']
    run_env['ANTHROPIC_API_KEY'] = cfg['token']
    run_env['ANTHROPIC_BASE_URL'] = cfg['base_url']
    run_env['ANTHROPIC_MODEL'] = leader_model
    run_env['HERMES_COLLAB_LEADER_MODEL'] = leader_model
    run_env['HERMES_COLLAB_WORKER_MODEL'] = worker_model

    server_cmd = [
        str(ROOT / 'hermes-collab'), 'server', '--host', host, '--port', str(port),
        '--cwd', cwd, '--db', str(ROOT / 'data' / 'collab.sqlite3'),
        '--leader-model', leader_model, '--worker-model', worker_model,
    ]

    print('\n启动配置：')
    safe_runtime = {k: v for k, v in runtime.items() if k != 'token'}
    print(json.dumps(safe_runtime, ensure_ascii=False, indent=2))

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
