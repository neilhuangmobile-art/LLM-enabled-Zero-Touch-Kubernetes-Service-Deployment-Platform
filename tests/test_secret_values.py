"""密碼類環境變數改成「名稱或值」判斷（2026-10-24）：DATABASE_URL 這種名稱看不出來、值裡帶帳號密碼的，
也要存成 Kubernetes Secret，不寫進部署設定與 Git。純邏輯，CI 會跑。"""
from agents.compose_import import looks_like_secret, parse_compose
from gitops.manifest_writer import split_secret_env, env_entries


def test_value_with_credentials_is_secret():
    assert looks_like_secret("DATABASE_URL", "postgres://app:s3cret@db:5432/shop")
    assert looks_like_secret("REDIS_URL", "redis://default:pw@cache:6379/0")
    assert looks_like_secret("CERT", "-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
    assert looks_like_secret("DB_PASSWORD", "x")                 # 名稱判斷照舊


def test_plain_values_not_secret():
    for k, v in (("API_BASE", "https://api.example.com/v1"), ("UPSTREAM", "http://web:80"),
                 ("EMAIL", "ops@example.com"), ("LOG_LEVEL", "debug"), ("DSN", "postgres://db:5432/shop")):
        assert not looks_like_secret(k, v), k


def test_split_and_env_entries_move_credential_url_to_secret():
    env = {"DATABASE_URL": "postgres://app:s3cret@db/shop", "LOG_LEVEL": "info"}
    plain, secret = split_secret_env(env)
    assert plain == {"LOG_LEVEL": "info"} and secret == {"DATABASE_URL": "postgres://app:s3cret@db/shop"}
    entries = env_entries("shop", env)
    assert "s3cret" not in repr(entries)
    assert {"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": "shop-secrets", "key": "DATABASE_URL"}}} in entries


def test_compose_import_marks_credential_url():
    doc = """
services:
  api:
    image: node:20-alpine
    environment:
      DATABASE_URL: postgres://app:s3cret@db/shop
      LOG_LEVEL: info
"""
    svc = parse_compose(doc)["services"][0]
    texts = " ".join(p["text"] for p in svc["problems"])
    assert "DATABASE_URL" in texts and "Secret" in texts and "LOG_LEVEL" not in texts
