
# Run pre-commit hooks on all files
lint:
	@echo "Running pre-commit linting on all files..."
	pre-commit run --all-files

# Run pre-commit hooks on changed files
lint-changed-files:
	@echo "Running pre-commit lint on changed files..."
	pre-commit run

# Update pre-commit hooks to the latest versions
autoupdate:
	@echo "Updating pre-commit hooks..."
	pre-commit autoupdate

# Run a specific hook (e.g., flake8, black, isort, etc.)
run-lint-hook:
	@echo "Running specific hook: $(hook)"
	pre-commit run $(hook)

.PHONY: lint lint-changed-files autoupdate run-lint-hook
