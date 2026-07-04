.PHONY: test up down build backup restore-drill run-local

test:
	pytest

up:
	docker compose up --build

run-local:
	pwsh -File ops/run_local.ps1

down:
	docker compose down

build:
	docker compose build --no-cache

backup:
	bash ops/backup.sh

restore-drill:
	bash ops/restore_drill.sh
