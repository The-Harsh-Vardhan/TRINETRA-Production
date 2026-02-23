"""Stream Ingestor configuration via environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379"
    FRAME_BUFFER_MAXLEN: int = 100       # Max entries per Redis Stream key
    TARGET_FPS: int = 15                 # Default inference FPS (overridden per camera)
    CAMERA_CONFIGS: str = "/etc/trinetra/cameras.yaml"
    METRICS_PORT: int = 8001

    class Config:
        env_file = ".env"
