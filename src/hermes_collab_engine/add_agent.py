from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, urlunparse

ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_CONFIG_PATH = ROOT / '.runtime-config.json'
HERMES_DIR = Path.home() / '.hermes'

SYSTEM_PROMPT = """You are an expert in AI coding tool API configurations.

Given a coding AI agent name, research its API format and return a JSON config.
The agent is used as a subprocess worker in a multi-agent system.

Rules for determining config:
- "api_format": "openai" if it uses OpenAI-compatible API (Chat Completions format)
- "api_format": "anthropic" if it uses Anthropic Messages API
- "env_var": the primary env var for the API key (e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY)
- "base_url_env": the env var for the base URL (e.g. ANTHROPIC_BASE_URL, OPENAI_BASE_URL), or null
- "fields": which fields to prompt for, order matters. Usually ["base_url", "api_key", "model"] or ["api_key", "model"]
- "optional_fields": which fields are optional, e.g. ["base_url"]
- "needs_proxy": true ONLY if the agent speaks a NON-OpenAI format (Anthropic, Google, etc.) that needs protocol translation
- "model_mappings": optional, if the agent uses model names that need translation
- "cli_command": the CLI binary name used to invoke the agent (e.g. "cursor", "windsurf")
- "prompt_flag": how to pass the prompt ("-p", "--prompt", or "" for positional)
- "supports_model_flag": whether the agent accepts --model argument
- "output_parser": "raw_text" for simple output, "claude_json" for Claude JSON format, "codex_json" for Codex JSON format
- "capabilities": list of capabilities like ["file-edit", "git-ops", "search", "test-run"]
- "install_commands": optional list of shell commands to install the agent CLI, first one preferred

Return ONLY valid JSON, no markdown, no explanation:
{
  "name": "agent_name",
  "display_name": "Display Name",
  "description": "Brief description of the agent",
  "api_format": "openai",
  "env_var": "OPENAI_API_KEY",
  "base_url_env": "OPENAI_BASE_URL",
  "fields": ["api_key", "model"],
  "optional_fields": ["base_url"],
  "needs_proxy": false,
  "model_mappings": {},
  "cli_command": "cursor",
  "prompt_flag": "--prompt",
  "supports_model_flag": true,
  "output_parser": "raw_text",
  "capabilities": ["file-edit", "search"],
  "install_commands": ["npm install -g @cursor/cli"]
}"""


def _load_runtime_config() -> dict:
    if not RUNTIME_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(RUNTIME_CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _call_llm(prompt: str, api_key: str, base_url: str, model: str) -> str:
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'x-opencode-client': 'opc-add-agent',
        'User-Agent': 'OpenCode/1.0',
    }
    body = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': prompt},
        ],
        'stream': False,
        'temperature': 0.1,
    }).encode()

    url = base_url.rstrip('/')
    if not url.endswith('/chat/completions'):
        parsed = urlparse(url)
        path = parsed.path.rstrip('/')
        if '/v1' not in path:
            path += '/v1/chat/completions'
        else:
            path += '/chat/completions'
        parsed = parsed._replace(path=path)
        url = urlunparse(parsed)

    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors='replace')[:500]
        print(f'  LLM 调用失败 (HTTP {e.code}): {err_body}')
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f'  无法连接 {url}: {e.reason}')
        sys.exit(1)

    content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
    if not content:
        print('  LLM 返回空，无法解析')
        sys.exit(1)

    content = content.strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
    return content.strip()


def _build_prompt(agent_name: str, user_hint: str = '') -> str:
    known_examples = """
Known agents for reference:
- claude-code: format=anthropic, env=ANTHROPIC_API_KEY, needs_proxy=true
- codex: format=openai, env=OPENAI_API_KEY, needs_proxy=true
- opencode: format=openai, env=OPENCODE_API_KEY, needs_proxy=false
- hermes: format=anthropic, env=ANTHROPIC_API_KEY, needs_proxy=false
"""
    pm = _available_package_managers()
    pm_info = f'\nAvailable package managers on this system: {", ".join(sorted(pm))}' if pm else '\nNo common package managers found on this system.'
    hint = f'\nAdditional hint from user: {user_hint}' if user_hint else ''
    return f'Research the AI coding agent "{agent_name}" and return its API config.{known_examples}{pm_info}{hint}'


def generate_config(agent_name: str, user_hint: str = '') -> dict:
    runtime = _load_runtime_config()
    if not runtime:
        print('未找到 .runtime-config.json，请先运行 start.py 完成配置')
        print('或者通过环境变量 OPENCODE_API_KEY / ANTHROPIC_API_KEY 和 OPENCODE_BASE_URL / ANTHROPIC_BASE_URL 提供凭据')
        api_key = os.environ.get('OPENCODE_API_KEY') or os.environ.get('ANTHROPIC_API_KEY') or ''
        base_url = os.environ.get('OPENCODE_BASE_URL') or os.environ.get('ANTHROPIC_BASE_URL') or 'https://opencode.ai/zen/go'
        model = os.environ.get('OPENCODE_MODEL') or 'deepseek-v4-flash'
        if not api_key:
            print('错误：无 API key，无法调用 LLM')
            sys.exit(1)
        print(f'  使用环境变量: base_url={base_url}, model={model}')
    else:
        provider = (runtime.get('providers') or [{}])[0]
        api_key = provider.get('api_key') or runtime.get('leader', {}).get('api_key', '')
        base_url = provider.get('base_url') or runtime.get('leader', {}).get('base_url', 'https://opencode.ai/zen/go')
        model = provider.get('default_model') or runtime.get('leader', {}).get('model', 'deepseek-v4-flash')
        print(f'  使用 runtime 配置: base_url={base_url}, model={model}')

    print(f'\n🔍 正在搜索 agent "{agent_name}" 的 API 配置...')
    prompt = _build_prompt(agent_name, user_hint)
    raw = _call_llm(prompt, api_key, base_url, model)

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f'  LLM 返回无法解析: {e}')
        print(f'  原始返回:\n{raw[:500]}')
        sys.exit(1)

    required_keys = ['name', 'api_format', 'env_var', 'fields', 'needs_proxy']
    for k in required_keys:
        if k not in config:
            print(f'  LLM 返回缺少必需字段: {k}')
            sys.exit(1)

    config.setdefault('optional_fields', [])
    config.setdefault('base_url_env', None)
    config.setdefault('model_mappings', {})
    config.setdefault('description', '')
    config.setdefault('cli_command', config['name'])
    config.setdefault('prompt_flag', '-p' if config['api_format'] == 'anthropic' else '--prompt')
    config.setdefault('supports_model_flag', True)
    config.setdefault('output_parser', 'raw_text')
    config.setdefault('capabilities', [])
    return config


def _make_schema_entry(config: dict) -> dict:
    return {
        'fields': config['fields'],
        'optional_fields': config.get('optional_fields', []),
        'format': config['api_format'],
        'env_var': config['env_var'],
        'base_url_env': config.get('base_url_env') or '',
    }


def _find_cli(name: str) -> bool:
    for p in os.environ.get('PATH', '').split(os.pathsep):
        candidate = os.path.join(p, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return True
    return False


def _available_package_managers() -> dict[str, str]:
    mgrs = {
        'npm': 'npm install -g {pkg}',
        'pip3': 'pip3 install {pkg}',
        'pip': 'pip install {pkg}',
        'go': 'go install {pkg}@latest',
        'cargo': 'cargo install {pkg}',
        'brew': 'brew install {pkg}',
    }
    return {k: v for k, v in mgrs.items() if _find_cli(k)}


def _make_backend_entry(config: dict) -> str:
    name = config['name']
    display = config.get('display_name', name.title())
    cmd = config.get('cli_command', name)
    pflag = config.get('prompt_flag', '-p')
    model_flag = config.get('model_flag', '--model')
    supports_model = config.get('supports_model_flag', True)
    supports_model_py = 'True' if supports_model else 'False'
    output_parser = config.get('output_parser', 'raw_text')
    capabilities = config.get('capabilities', [])
    caps_str = json.dumps(capabilities) if capabilities else '[]'

    return f'''_register_builtin(AgentBackend(
    name="{name}",
    display_name="{display}",
    command=["{cmd}"],
    prompt_flag="{pflag}",
    output_format_flags=[],
    supports_model_flag={supports_model_py},
    model_flag="{model_flag}",
    permission_flags=None,
    allowed_tools_flag=None,
    output_parser="{output_parser}",
    process_pattern="{cmd}",
    prompt_prefix="You are a {display} worker in a Hermes collaboration engine.",
    prompt_suffix="",
    default_allowed_tools=[],
    capabilities={caps_str},
    reasoning_flags=[],
    reasoning_env={{}},
))'''


def apply_config(config: dict) -> None:
    name = config['name']
    needs_proxy = config.get('needs_proxy', False)
    schema = _make_schema_entry(config)

    print(f'\n📝 正在写入配置...')

    start_py = ROOT / 'start.py'
    if start_py.exists():
        content = start_py.read_text(encoding='utf-8')

        marker = "ADAPTER_FIELD_SCHEMA = {"
        idx = content.find(marker)
        if idx != -1:
            brace_idx = idx + len(marker)
            entry_lines = f"\n    '{name}': {json.dumps(schema, ensure_ascii=False, indent=4).replace(chr(10), chr(10) + '    ')},\n"
            content = content[:brace_idx] + entry_lines + content[brace_idx:]

            if needs_proxy:
                proxy_marker = '_ADAPTERS_NEED_PROXY = {'
                pidx = content.find(proxy_marker)
                if pidx != -1:
                    pbrace = pidx + len(proxy_marker)
                    content = content[:pbrace] + f"'{name}', " + content[pbrace:]

            start_py.write_text(content, encoding='utf-8')
            print(f'  ✓ 已写入 ADAPTER_FIELD_SCHEMA["{name}"] → start.py')
        else:
            print(f'  ⚠ 找不到 ADAPTER_FIELD_SCHEMA')

    agents_py = ROOT / 'src' / 'hermes_collab_engine' / 'agents.py'
    if agents_py.exists():
        ac = agents_py.read_text(encoding='utf-8')
        insert_marker = 'def list_backends() -> list[AgentBackend]:'
        aidx = ac.find(insert_marker)
        if aidx != -1:
            backend_entry = _make_backend_entry(config)
            ac = ac[:aidx] + backend_entry + '\n\n\n' + ac[aidx:]
            agents_py.write_text(ac, encoding='utf-8')
            print(f'  ✓ 已注册 AgentBackend["{name}"] → agents.py')
        else:
            print(f'  ⚠ 找不到 agents.py 插入点')

    if config.get('model_mappings'):
        handler_go = ROOT / 'proxy' / 'internal' / 'proxy' / 'handler.go'
        if handler_go.exists():
            hc = handler_go.read_text(encoding='utf-8')
            alias_marker = 'var modelAliases = map[string]string{'
            aidx = hc.find(alias_marker)
            if aidx != -1:
                abrace = aidx + len(alias_marker)
                alias_lines = ''
                for src, dst in config['model_mappings'].items():
                    alias_lines += f'\n\t"{src}":    "{dst}",'
                hc = hc[:abrace] + alias_lines + hc[abrace:]
                handler_go.write_text(hc, encoding='utf-8')
                print(f'  ✓ 已写入 modelAliases 映射 → handler.go')
        else:
            print(f'  ⚠ 找不到 handler.go，请手动添加 model aliases')
    else:
        print(f'  · 无需模型别名映射')

    print(f'\n✅ agent "{name}" 已添加完成')
    print(f'   下次运行 start.py/opc 时即可在 Worker Agent 列表中看到 "{name}"')
    if needs_proxy:
        print(f'   启动后 Worker 会自动走代理 (协议翻译)')
    else:
        print(f'   启动后 Worker 会直连上游')


def add_agent(agent_name: str, user_hint: str = '', auto_apply: bool = False) -> None:
    config = generate_config(agent_name, user_hint)

    print(f'\n📋 发现配置:')
    print(json.dumps(config, ensure_ascii=False, indent=2))

    cli_cmd = config.get('cli_command', config['name'])
    if not _find_cli(cli_cmd):
        install_cmds = config.get('install_commands', [])
        print(f'\n⚠️  CLI "{cli_cmd}" 未在 PATH 中找到')
        if install_cmds:
            print(f'   建议安装:')
            for ic in install_cmds:
                print(f'     {ic}')
        raw = input(f'\n是否尝试安装? [y/N]: ').strip().lower()
        if raw in ('y', 'yes'):
            install_attempts = list(install_cmds)
            available_pms = _available_package_managers()
            if not install_attempts:
                for pm, template in available_pms.items():
                    install_attempts.append(template.format(pkg=cli_cmd))
            import subprocess as _sp
            _ALLOWED_PREFIXES = ("npm install", "pip install", "pip3 install", "go install", "cargo install", "brew install")
            for ic in install_attempts:
                pm_name = ic.split()[0]
                if pm_name in available_pms or pm_name not in ('npm', 'pip', 'pip3', 'go', 'cargo', 'brew'):
                    if not ic.startswith(_ALLOWED_PREFIXES):
                        print(f'  ⛔ blocked (not in whitelist): {ic}')
                        continue
                    print(f'  正在运行: {ic}')
                    ret = _sp.run(ic, shell=True, check=False).returncode
                    if ret == 0 and _find_cli(cli_cmd):
                        print(f'  ✓ {cli_cmd} 已安装')
                        break
                    print(f'  ⚠ 失败 (exit={ret})')
                else:
                    print(f'  ⚠ 跳过 {ic} (需要 {pm_name}，但系统中未找到)')
            else:
                print(f'  ⚠ 所有安装方式均失败，请手动安装 {cli_cmd}')
                if not auto_apply:
                    if input('\n继续注册? [y/N]: ').strip().lower() not in ('y', 'yes'):
                        print('已取消')
                        return
        elif not auto_apply:
            raw2 = input(f'\n是否仍要注册? [y/N]: ').strip().lower()
            if raw2 not in ('y', 'yes'):
                print('已取消')
                return
    else:
        print(f'  ✓ CLI "{cli_cmd}" 已安装')

    if not auto_apply:
        raw = input(f'\n是否应用此配置? [Y/n]: ').strip().lower()
        if raw and raw not in ('y', 'yes', ''):
            print('已取消')
            return

    apply_config(config)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description='自动发现并注册新的 Worker Agent')
    parser.add_argument('name', help='Agent 名称，如 cursor, windsurf')
    parser.add_argument('--hint', '-H', default='', help='额外提示，帮助 LLM 更精确识别')
    parser.add_argument('--yes', '-y', action='store_true', help='直接应用，不询问')
    args = parser.parse_args()

    add_agent(args.name.lower(), args.hint, auto_apply=args.yes)
    return 0


if __name__ == '__main__':
    sys.exit(main())
