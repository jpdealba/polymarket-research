.PHONY: test up down build backup restore-drill

test:
	pytest

up:
	docker compose up --build

down:
	docker compose down

build:
	docker compose build --no-cache

backup:
	bash ops/backup.sh

restore-drill:
	bash ops/restore_drill.sh
