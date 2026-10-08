"""Encrypt persisted API credentials with a key kept outside the database."""
import copy
import os
import threading
from cryptography.fernet import Fernet, InvalidToken
from ..config import settings

_key_lock = threading.Lock()


class SecretStorageError(RuntimeError):
    pass


def cipher(create=False):
    cfg = settings()
    try:
        if cfg.model_encryption_key:
            key = cfg.model_encryption_key.encode()
        else:
            path = cfg.model_key_file
            with _key_lock:
                if not path.exists() and create:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as file:
                        file.write(Fernet.generate_key())
                key = path.read_bytes().strip()
        return Fernet(key)
    except (OSError, ValueError) as error:
        raise SecretStorageError('模型密钥文件不可用，请恢复原密钥文件') from error


def encrypt_configuration(config):
    stored = copy.deepcopy(config)
    for connection in stored['connections']:
        secret = connection.pop('api_key', '')
        connection.pop('api_key_encrypted', None)
        if secret:
            connection['api_key_encrypted'] = cipher(create=True).encrypt(secret.encode()).decode()
    return stored


def decrypt_configuration(stored):
    config = copy.deepcopy(stored)
    for connection in config['connections']:
        token = connection.pop('api_key_encrypted', None)
        if token:
            try:
                connection['api_key'] = cipher().decrypt(token.encode()).decode()
            except (InvalidToken, UnicodeError) as error:
                raise SecretStorageError('无法解密模型 API Key，请恢复原密钥文件') from error
        else:
            connection.setdefault('api_key', '')
    return config


def migrate_credentials(db):
    import json
    saved = db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
    if saved:
        config = json.loads(saved['value'])
        if any(connection.get('api_key') for connection in config['connections']):
            from ..db import dumps
            db.execute("UPDATE app_settings SET value=? WHERE name='models'", (dumps(encrypt_configuration(decrypt_configuration(config))),))
