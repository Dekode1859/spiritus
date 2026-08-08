.PHONY: run install auth-setup auth-status

install:
	uv sync

run:
	uv run python main.py

# Add an AI provider credential to the app-local opencode home.
# Credentials are stored in .opencode-home/ — isolated from your global opencode.
auth-setup:
	@mkdir -p .opencode-home
	HOME="$(CURDIR)/.opencode-home" opencode providers login

auth-status:
	@mkdir -p .opencode-home
	HOME="$(CURDIR)/.opencode-home" opencode providers list
