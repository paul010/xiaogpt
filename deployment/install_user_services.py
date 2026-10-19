"""Install independent user services for two speakers without copying credentials."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def quote(value: Path | str) -> str:
    value = str(value)
    if any(c in value for c in "\n\r\0"):
        raise ValueError("A systemd path must be a single line")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def render_unit(root: Path, python: Path, config: Path) -> str:
    # %i is our own systemd instance specifier; user-supplied % is escaped.
    config_arg = quote(config / "INSTANCE.yaml").replace("INSTANCE.yaml", "%i.yaml")
    token_env = quote("XIAOGPT_MI_TOKEN_PATH=" + str(config / "INSTANCE.mi.token")).replace("INSTANCE.mi.token", "%i.mi.token")
    quote(root)  # Reject line breaks before rendering a single-value directive.
    working_dir = str(root).replace("%", "%%")
    quote(config)
    env_file = str(config / "common.env").replace("%", "%%")
    return f"""[Unit]
Description=XiaoGPT OpenRouter speaker %i
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory={working_dir}
ExecStart={quote(python)} -u {quote(root / 'xiaogpt.py')} --config {config_arg}
Environment=PYTHONUNBUFFERED=1
Environment={token_env}
EnvironmentFile=-{env_file}
Restart=on-failure
RestartSec=15
TimeoutStopSec=15
UMask=0077

[Install]
WantedBy=default.target
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path.home() / ".config/xiaogpt")
    parser.add_argument("--enable", action="store_true", help="Enable and restart both instances")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = args.config_dir.expanduser().absolute()
    # Do not resolve the venv executable symlink: that would select system Python.
    python = Path(os.path.abspath(sys.executable))
    for name in ("bedroom", "livingroom"):
        p = config / f"{name}.yaml"
        if not p.is_file():
            parser.error(f"Create private configuration first: {p}")
        token = config / f"{name}.mi.token"
        original_token = Path.home() / ".mi.token"
        if not token.exists() and original_token.is_file():
            token.write_bytes(original_token.read_bytes())
        if token.exists():
            token.chmod(0o600)
    config.chmod(0o700)
    for p in (config / "bedroom.yaml", config / "livingroom.yaml", config / "common.env"):
        if p.exists():
            p.chmod(0o600)
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "xiaogpt@.service"
    rendered = render_unit(root, python, config)
    # Validate before replacing an existing, potentially running service.
    with tempfile.TemporaryDirectory(prefix="xiaogpt-unit-") as directory:
        candidate = Path(directory) / unit.name
        candidate.write_text(rendered)
        subprocess.run(["systemd-analyze", "--user", "verify", str(candidate)], check=True)
    if unit.exists():
        backup = unit.with_suffix(".service.previous")
        backup.write_bytes(unit.read_bytes())
    unit.write_text(rendered)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if args.enable:
        services = [f"xiaogpt@{name}.service" for name in ("bedroom", "livingroom")]
        subprocess.run(["systemctl", "--user", "enable", *services], check=True)
        subprocess.run(["systemctl", "--user", "restart", *services], check=True)
    print(f"Installed {unit}; credentials remain outside the repository.")
    print("For operation after logout: loginctl enable-linger <your-linux-user>")


if __name__ == "__main__":
    main()
