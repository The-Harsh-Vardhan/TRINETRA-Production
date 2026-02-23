"""Inference Worker configuration."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    REDIS_URL: str = "redis://localhost:6379"
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    YOLO_ENGINE_PATH: str = "/models/yolov8m.engine"
    ARCFACE_ENGINE_PATH: str = "/models/arcface_r50.engine"
    BATCH_SIZE: int = 4
    BATCH_TIMEOUT_MS: float = 20.0
    METRICS_PORT: int = 8002

    class Config:
        env_file = ".env"
