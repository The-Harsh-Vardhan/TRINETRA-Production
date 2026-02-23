# TRINETRA — Makefile
# Shortcut commands for development and deployment

.PHONY: up down logs ps build clean help kafka-topics redis-cli

help:
	@echo "TRINETRA Production Stack"
	@echo "--------------------------"
	@echo "make up         - Start all services"
	@echo "make down       - Stop all services"
	@echo "make build      - Rebuild all service images"
	@echo "make logs       - Tail all service logs"
	@echo "make ps         - Show running services"
	@echo "make clean      - Remove containers + volumes (DESTRUCTIVE)"
	@echo "make kafka-topics - List Kafka topics"
	@echo "make redis-cli  - Open Redis CLI"
	@echo "make grafana    - Open Grafana in browser"

up:
	docker compose up -d
	@echo "Services starting... Check status with: make ps"

down:
	docker compose down

build:
	docker compose build --parallel

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

clean:
	@echo "WARNING: This will delete all volumes including model cache and Qdrant data."
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 1
	docker compose down -v --remove-orphans

# ── Development shortcuts ─────────────────────────────────────────────────────

kafka-topics:
	docker compose exec kafka kafka-topics --bootstrap-server localhost:9092 --list

kafka-consumer-lag:
	docker compose exec kafka kafka-consumer-groups \
		--bootstrap-server localhost:9092 \
		--describe --all-groups

redis-cli:
	docker compose exec redis redis-cli -a $${REDIS_PASSWORD:-trinetra_redis_secret}

redis-stream-lengths:
	docker compose exec redis redis-cli -a $${REDIS_PASSWORD:-trinetra_redis_secret} \
		--scan --pattern "frames:*" | xargs -I {} \
		docker compose exec redis redis-cli -a $${REDIS_PASSWORD:-trinetra_redis_secret} xlen {}

grafana:
	@echo "Opening Grafana at http://localhost:3000 (admin/trinetra_grafana)"
	start http://localhost:3000

qdrant-collections:
	curl -s http://localhost:6333/collections | python -m json.tool

# ── Model Export (run once per GPU SKU) ──────────────────────────────────────

export-yolo:
	@echo "Exporting YOLOv8m to TensorRT engine (requires GPU)..."
	docker run --gpus all --rm \
		-v $$(pwd)/models:/models \
		nvcr.io/nvidia/tensorrt:23.10-py3 \
		python -c "from ultralytics import YOLO; \
			m = YOLO('yolov8m.pt'); \
			m.export(format='engine', imgsz=640, half=True, workspace=4)"

# ── Environment ───────────────────────────────────────────────────────────────

env:
	cp .env.example .env
	@echo ".env created. Edit secrets before running 'make up'."
