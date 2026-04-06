import yaml
import os
from dotenv import load_dotenv
from pathlib import Path

# 프로젝트 루트 경로
ROOT_DIR = Path(__file__).parent.parent.parent
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"


def load_config() -> dict:
    """settings.yaml 로드"""
    config_path = CONFIG_DIR / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env():
    """환경변수 로드 (.env)"""
    env_path = ROOT_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def get_env(key: str, default: str = None) -> str:
    """환경변수 조회"""
    load_env()
    value = os.getenv(key, default)
    if value is None:
        raise ValueError(f"환경변수 {key}가 설정되지 않았습니다. .env 파일을 확인하세요.")
    return value
