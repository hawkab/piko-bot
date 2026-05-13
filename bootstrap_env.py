import json
import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

WORKSPACE = Path(os.getenv("PIKOBOT_WORKSPACE", "/root/.pikobot/workspace")).expanduser()
RUNTIME_DIR = WORKSPACE / "runtime"
ENV_JSON = RUNTIME_DIR / "environment.json"
USER_MD = WORKSPACE / "USER.md"
TOOLS_MD = WORKSPACE / "TOOLS.md"

COMMANDS_TO_CHECK = [
    "sh",
    "bash",
    "python",
    "python3",
    "pip",
    "pip3",
    "apt",
    "apt-get",
    "dpkg",
    "curl",
    "wget",
    "git",
    "ss",
    "netstat",
    "ps",
    "grep",
    "sed",
    "awk",
    "find",
    "tar",
    "gzip",
    "nginx",
    "systemctl",
    "service",
    "supervisorctl",
    "docker",
    "docker-compose",
]

PACKAGE_PROBES = [
    "nginx",
    "curl",
    "wget",
    "git",
    "procps",
    "iproute2",
]


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
        return completed.returncode, completed.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def read_os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    data: dict[str, str] = {}
    if not path.exists():
        return data

    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def detect_commands() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for cmd in COMMANDS_TO_CHECK:
        result[cmd] = shutil.which(cmd)
    return result


def detect_packages(commands: dict[str, str | None]) -> dict[str, str]:
    result: dict[str, str] = {}

    if commands.get("dpkg"):
        for pkg in PACKAGE_PROBES:
            code, out = run(["dpkg", "-s", pkg])
            result[pkg] = "installed" if code == 0 else "not-installed"
    else:
        for pkg in PACKAGE_PROBES:
            result[pkg] = "unknown"

    return result


def detect_python_packages() -> list[str]:
    code, out = run(["python3", "-m", "pip", "list", "--format=json"])
    if code != 0:
        return []

    try:
        raw = json.loads(out)
        if not isinstance(raw, list):
            return []
        names = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                version = str(item.get("version", "")).strip()
                if name:
                    names.append(f"{name}=={version}" if version else name)
        return sorted(names)
    except Exception:
        return []


def collect_environment() -> dict[str, Any]:
    os_release = read_os_release()
    commands = detect_commands()
    packages = detect_packages(commands)
    python_packages = detect_python_packages()

    env = {
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "os_release": os_release,
        "commands": commands,
        "packages": packages,
        "python_packages": python_packages,
        "workspace": str(WORKSPACE),
        "rules": {
            "systemctl_available": bool(commands.get("systemctl")),
            "service_available": bool(commands.get("service")),
            "apt_available": bool(commands.get("apt") or commands.get("apt-get")),
            "dpkg_available": bool(commands.get("dpkg")),
            "docker_available": bool(commands.get("docker")),
        },
    }
    return env


def write_json(data: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    ENV_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_user_md(data: dict[str, Any]) -> str:
    os_release = data.get("os_release", {}) or {}
    platform_info = data.get("platform", {}) or {}
    rules = data.get("rules", {}) or {}

    distro = os_release.get("PRETTY_NAME") or os_release.get("NAME") or "unknown"

    return f"""# Runtime Environment

Этот файл сгенерирован автоматически при старте контейнера. Это источник фактов о текущем окружении.

## Базовые факты
- Дистрибутив: {distro}
- Система: {platform_info.get("system")}
- Архитектура: {platform_info.get("machine")}
- Python: {platform_info.get("python_version")}
- Workspace: {data.get("workspace")}

## Ключевые ограничения
- systemctl доступен: {"да" if rules.get("systemctl_available") else "нет"}
- service доступен: {"да" if rules.get("service_available") else "нет"}
- apt/apt-get доступен: {"да" if rules.get("apt_available") else "нет"}
- dpkg доступен: {"да" if rules.get("dpkg_available") else "нет"}
- docker доступен: {"да" if rules.get("docker_available") else "нет"}

## Обязательные правила
- Если что-то можно сделать без команд из недоступного списка, то сделай. Если невозможно, то сначаоа установи то чего не хватает
- Если задача сформулирована в том числе в том, чтобы установить недостающую команду, то установи её, например через стандартный пакетный менеджер, а если невозможно, то curl (если curl недоступен, то установи его тоже)
- Не строй план из гипотез. Сначала опирайся на этот файл и TOOLS.md.
- Если в окружении нет нужного инструмента, либо используй доступную альтернативу, либо верни БЛОКЕР, либо задай ВОПРОС.
"""


def build_tools_md(data: dict[str, Any]) -> str:
    commands = data.get("commands", {}) or {}
    packages = data.get("packages", {}) or {}

    available = []
    unavailable = []

    for name, path in commands.items():
        line = f"- {name}: {path}" if path else f"- {name}: недоступно"
        if path:
            available.append(line)
        else:
            unavailable.append(line)

    installed = []
    missing = []
    for name, state in packages.items():
        if state == "installed":
            installed.append(f"- {name}: installed")
        else:
            missing.append(f"- {name}: {state}")

    return """# Tools And Capabilities

Этот файл сгенерирован автоматически. Перед выполнением задачи опирайся на него как на фактический список доступных инструментов.

## Доступные команды
{available}

## Недоступные команды
{unavailable}

## Проверенные пакеты
{installed}

## Отсутствующие или неизвестные пакеты
{missing}

## Правила использования
- Если задача требует недоступного инструмента, используй доступную альтернативу.
- Если что-то невозможно сделать без команд из недоступного списка, то сначала установи то чего не хватает через стандартный пакетный менеджер (если невозможно, то через curl (если curl не установлен, то установи его тоже))
- Если задача сформулирована в том числе в том, чтобы установить недостающую команду, то установи её
- Если безопасной альтернативы нет, верни БЛОКЕР.
""".format(
        available="\n".join(available) if available else "- нет данных",
        unavailable="\n".join(unavailable) if unavailable else "- нет данных",
        installed="\n".join(installed) if installed else "- нет данных",
        missing="\n".join(missing) if missing else "- нет данных",
    )


def main() -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    data = collect_environment()
    write_json(data)
    USER_MD.write_text(build_user_md(data), encoding="utf-8")
    TOOLS_MD.write_text(build_tools_md(data), encoding="utf-8")
    print(f"[bootstrap_env] wrote {ENV_JSON}")
    print(f"[bootstrap_env] wrote {USER_MD}")
    print(f"[bootstrap_env] wrote {TOOLS_MD}")


if __name__ == "__main__":
    main()
