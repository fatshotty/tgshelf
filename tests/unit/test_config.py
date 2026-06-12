import dataclasses

import pytest

from tgshelf.config import ConfigError, load_config

MINIMAL_YAML = """
db: postgresql+asyncpg://u:p@localhost/tgshelf
telegram:
  upload:
    channel: -1001234567890
"""


def write_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_minimal_config_applies_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path, MINIMAL_YAML), env={})

    assert cfg.db == "postgresql+asyncpg://u:p@localhost/tgshelf"
    assert cfg.telegram.upload.channel == -1001234567890
    assert cfg.telegram.upload.min_size == 2 * 1024 * 1024
    assert cfg.telegram.users == ()
    assert cfg.session_storage == "db"
    assert cfg.logger == "info"
    assert cfg.download.multi_bot_download == 1
    assert cfg.download.allow_user_fallback is False
    assert cfg.download.chunk_timeout == 6.0
    assert cfg.download.memory_soft_limit == 0
    assert cfg.operations.batch == 10
    assert cfg.http.port == 3000
    assert cfg.strm.template == "http://127.0.0.1:3000/files/{file_id}"
    assert cfg.changes_feed.enabled is False


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", env={})


def test_missing_db_raises(tmp_path):
    yaml_text = """
telegram:
  upload:
    channel: -100123
"""
    with pytest.raises(ConfigError, match="db"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_env_db_overrides_yaml(tmp_path):
    env = {"DB": "postgresql+asyncpg://env-wins/tgshelf"}
    cfg = load_config(write_config(tmp_path, MINIMAL_YAML), env=env)
    assert cfg.db == "postgresql+asyncpg://env-wins/tgshelf"


def test_master_channel_required(tmp_path):
    yaml_text = "db: postgresql+asyncpg://u:p@localhost/tgshelf"
    with pytest.raises(ConfigError, match="telegram.upload.channel"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_min_size_must_be_multiple_of_part_size(tmp_path):
    yaml_text = MINIMAL_YAML + """
    min_size: 1000
"""
    with pytest.raises(ConfigError, match="min_size"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_invalid_session_storage_raises(tmp_path):
    yaml_text = MINIMAL_YAML + "session_storage: redis\n"
    with pytest.raises(ConfigError, match="session_storage"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_invalid_logger_level_raises(tmp_path):
    yaml_text = MINIMAL_YAML + "logger: verbose\n"
    with pytest.raises(ConfigError, match="logger"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_users_parsed_with_bot_detection(tmp_path):
    yaml_text = """
db: postgresql+asyncpg://u:p@localhost/tgshelf
telegram:
  users:
    - name: main
      api_id: "123456"
      api_hash: abcdef
    - name: bot01
      api_id: 123456
      api_hash: abcdef
      bot_token: "123:token"
  upload:
    channel: -100123
"""
    cfg = load_config(write_config(tmp_path, yaml_text), env={})

    main, bot = cfg.telegram.users
    assert main.name == "main"
    assert main.api_id == 123456  # coerced from string
    assert main.is_bot is False
    assert bot.is_bot is True
    assert bot.bot_token == "123:token"


def test_duplicate_account_names_raise(tmp_path):
    yaml_text = """
db: postgresql+asyncpg://u:p@localhost/tgshelf
telegram:
  users:
    - {name: main, api_id: 1, api_hash: a}
    - {name: main, api_id: 2, api_hash: b}
  upload:
    channel: -100123
"""
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_account_missing_api_hash_raises(tmp_path):
    yaml_text = """
db: postgresql+asyncpg://u:p@localhost/tgshelf
telegram:
  users:
    - {name: main, api_id: 1}
  upload:
    channel: -100123
"""
    with pytest.raises(ConfigError, match="api_hash"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_multi_bot_download_must_be_positive(tmp_path):
    yaml_text = MINIMAL_YAML + """
download:
  multi_bot_download: 0
"""
    with pytest.raises(ConfigError, match="multi_bot_download"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_negative_memory_soft_limit_raises(tmp_path):
    yaml_text = MINIMAL_YAML + """
download:
  memory_soft_limit: -1
"""
    with pytest.raises(ConfigError, match="memory_soft_limit"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_invalid_cidr_in_ignore_auth_for_raises(tmp_path):
    yaml_text = MINIMAL_YAML + """
http:
  ignore_auth_for: ["not-a-network"]
"""
    with pytest.raises(ConfigError, match="ignore_auth_for"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_strm_template_with_custom_placeholders(tmp_path):
    yaml_text = MINIMAL_YAML + """
strm:
  template: "http://h/files/{file_id}?{channel_id}|{parts}"
"""
    cfg = load_config(write_config(tmp_path, yaml_text), env={})
    assert cfg.strm.template == "http://h/files/{file_id}?{channel_id}|{parts}"


def test_strm_template_unknown_placeholder_raises(tmp_path):
    yaml_text = MINIMAL_YAML + """
strm:
  template: "http://h/files/{node}"
"""
    with pytest.raises(ConfigError, match="placeholder"):
        load_config(write_config(tmp_path, yaml_text), env={})


def test_config_is_frozen(tmp_path):
    cfg = load_config(write_config(tmp_path, MINIMAL_YAML), env={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.db = "other"
