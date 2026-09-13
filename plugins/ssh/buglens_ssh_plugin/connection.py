"""Shared connection setup; strict host keys and credentials stay local."""

from pathlib import Path

SSH_PROPERTIES = {
    "host": {"type": "string", "minLength": 1, "maxLength": 512},
    "port": {"type": "integer", "minimum": 1, "maximum": 65535, "default": 22},
    "known_hosts": {
        "type": "string",
        "title": "主机指纹文件",
        "description": "后端服务器上的 known_hosts 文件；留空使用系统指纹库。",
        "maxLength": 4096,
    },
    "identity_file": {
        "type": "string",
        "title": "SSH 私钥文件",
        "description": "后端服务器上的私钥路径；也可使用 SSH Agent 或下方密码。",
        "maxLength": 4096,
    },
}


def validate_connection(config):
    host = config.get("host")
    if (
        not isinstance(host, str)
        or not host.strip()
        or any(char.isspace() for char in host)
        or "\x00" in host
    ):
        raise ValueError("SSH host must be a single hostname")
    for key in ("known_hosts", "identity_file"):
        value = config.get(key)
        if value is not None and (
            not isinstance(value, str) or not value.strip() or "\x00" in value
        ):
            raise ValueError("invalid SSH file path")


def connect_ssh(config, timeout):
    import paramiko

    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
        if config.get("known_hosts"):
            client.load_host_keys(str(Path(config["known_hosts"]).expanduser()))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=config["host"],
            port=config.get("port", 22),
            username=config.get("username"),
            password=config.get("password"),
            key_filename=str(Path(config["identity_file"]).expanduser())
            if config.get("identity_file")
            else None,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            channel_timeout=timeout,
            allow_agent=True,
            look_for_keys=True,
        )
        return client
    except Exception:
        client.close()
        raise
