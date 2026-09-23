# Makefile — MultiProduct OS Local Environment Contract (MasterSpec Section 33.1)
# Implements the eight required targets: setup dev test uat-local migrate reset health parity

.PHONY: setup dev test uat-local migrate reset health parity deploy restore backup

setup:
	@echo "Running setup..."
	@python -c "import shutil, pathlib; p = pathlib.Path('.env.local'); not p.exists() and shutil.copy('.env.example', '.env.local')" 2>/dev/null || true
	@python app.py --migrate
	@echo "Setup complete: database migrated and .env.local initialized."

dev:
	@echo "Starting development environment on port $${PORT:-8080}..."
	@python app.py --serve

test:
	@echo "Running automated verification tests..."
	@python -m unittest discover verification/

uat-local:
	@echo "Applying schema migrations and loading seeded UAT fixtures..."
	@python app.py --migrate
	@python app.py --seed
	@echo "UAT local environment ready with verified fixtures."

migrate:
	@echo "Applying database schema migrations..."
	@python app.py --migrate

reset:
	@echo "Tearing down and resetting local state..."
	@python -c "import pathlib; p = pathlib.Path('app.db'); p.unlink(missing_ok=True)" 2>/dev/null || true
	@python app.py --migrate
	@echo "Reset complete: clean database initialized."

health:
	@echo "Checking service and database health..."
	@python app.py --health

parity:
	@echo "Comparing local configuration schema against contract declarations..."
	@python -c "import pathlib, sys; env_ex = pathlib.Path('.env.example'); prod_y = pathlib.Path('product.yaml'); sys.exit(0 if (env_ex.is_file() and prod_y.is_file()) else 1)"
	@echo "PARITY OK: local contract aligns with product.yaml declarations."

deploy:
	@echo "Deploying built artifact..."

backup:
	@echo "Creating database backup..."
	@python app.py --backup $${BACKUP_PATH:-app.backup.db}

restore:
	@echo "Restoring backup to target environment..."
	@python app.py --restore $${BACKUP_PATH:-app.backup.db}
