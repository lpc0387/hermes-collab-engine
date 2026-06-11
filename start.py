#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLAUDE_DIR = Path.home() / '.claude'

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


def collect_profiles():
    profiles = []
    settings = CLAUDE_DIR / 'settings.json'
    if settings.exists():
        profiles.append({'name': 'Claude Code 当前配置', 'path': str(settings), 'data': load_json_lenient(settings)})
    profiles_dir = CLAUDE_DIR / 'profiles'
    if profiles_dir.exists():
        for p in sorted(profiles_dir.glob('*.json')):
            try:
                profiles.append({'name': p.stem, 'path': str(p), 'data': load_json_lenient(p)})
            except Exception as e:
                print(f'跳过无法读取的配置 {p}: {e}')
    return profiles


def unique(items):
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def models_from(profile):
    data = profile['data']
    env = data.get('env', {})
    models = []
    for key in ['ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL']:
        models.append(env.get(key))
    models.extend(data.get('availableModels') or [])
    return unique(models)


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


def get_config_from_files():
    profiles = collect_profiles()
    if not profiles:
        print('未找到 Claude/Hermes 本机配置文件。')
        return None
    labels = []
    for p in profiles:
        env = p['data'].get('env', {})
        labels.append(f"{p['name']} | {env.get('ANTHROPIC_BASE_URL','未设置 BaseURL')} | 模型数 {len(models_from(p))}")
    selected = choose('选择本机配置来源', labels)
    profile = profiles[labels.index(selected)]
    env = profile['data'].get('env', {})
    token = env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY')
    base_url = env.get('ANTHROPIC_BASE_URL')
    models = models_from(profile)
    if not token or not base_url:
        print('该配置缺少 BaseURL 或 API Key。')
        return None
    return {
        'source': profile['name'],
        'source_path': profile['path'],
        'base_url': base_url,
        'token': token,
        'models': models or DEFAULT_MODELS,
        'default_leader': env.get('ANTHROPIC_DEFAULT_OPUS_MODEL'),
        'default_worker': env.get('ANTHROPIC_DEFAULT_SONNET_MODEL'),
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
        'source': '手动输入',
        'source_path': '',
        'base_url': base_url,
        'token': token,
        'models': models,
        'default_leader': models[0],
        'default_worker': models[min(1, len(models)-1)],
    }


def stop_existing_server():
    subprocess.run(['pkill', '-f', 'src.hermes_collab_engine.cli server'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    print('⚕ Hermes 协同引擎启动器')
    mode = choose('API 配置方式', ['自动读取本机 Claude/Hermes 配置文件', '手动填写 BaseURL、API Key 和模型名称'], 1)
    cfg = get_config_from_files() if mode.startswith('自动') else get_config_manual()
    if cfg is None:
        print('自动读取失败，切换为手动填写。')
        cfg = get_config_manual()

    models = cfg['models']
    default_leader = models.index(cfg['default_leader']) + 1 if cfg.get('default_leader') in models else 1
    default_worker = models.index(cfg['default_worker']) + 1 if cfg.get('default_worker') in models else min(2, len(models))

    leader_model = choose('选择 Leader Agent（Hermes 命令行 / 规划与聚合大脑）模型', models, default_leader)
    worker_model = choose('选择 Worker Agent（Claude Code 执行器大脑）模型', models, default_worker)

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
    (ROOT / '.runtime-config.json').write_text(json.dumps(runtime, ensure_ascii=False, indent=2), encoding='utf-8')

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
    safe_runtime = dict(runtime)
    print(json.dumps(safe_runtime, ensure_ascii=False, indent=2))

    print('\n正在启动协同引擎管理面板...')
    stop_existing_server()
    log_path = ROOT / 'data' / 'server.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open('a', encoding='utf-8')
    server = subprocess.Popen(server_cmd, env=run_env, cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT)
    time.sleep(1.5)
    if server.poll() is not None:
        print(f'管理面板启动失败，请查看日志：{log_path}')
        return 1

    display_host = host if host != '0.0.0.0' else '服务器IP'
    print(f'管理面板已启动：http://{display_host}:{port}')
    print(f'服务日志：{log_path}')

    hermes_cmd = ['hermes', '--provider', 'anthropic', '--model', leader_model]
    print('\n正在进入 Hermes 命令行...')
    print('退出 Hermes 后，本启动脚本会停止本次启动的管理面板。\n')

    try:
        if os.environ.get('OPC_SKIP_HERMES') == '1':
            print('OPC_SKIP_HERMES=1，跳过进入 Hermes（用于测试启动脚本）。')
            while True:
                time.sleep(60)
        else:
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
