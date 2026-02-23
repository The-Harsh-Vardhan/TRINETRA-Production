w"""Identity Resolver configuration."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_CONSUMER_GROUP: str = "identity-resolver-group"
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = "trinetra_qdrant_secret"
    QDRANT_COLLECTION: str = "face_embeddings"
    COSINE_THRESHOLD: float = 0.72
    TEMPORAL_GATE_WINDOW_S: float = 3600.0   # 1 hour max in-store session
    METRICS_PORT: int = 8003

    class Config:
        env_file = ".env"
