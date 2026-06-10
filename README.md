# Options Quant

Production-oriented Python scaffold for an options research framework.

## Goals

- Research options strategies with a clean separation between domain logic, data access, analytics, backtesting, and reporting.
- Keep infrastructure concerns isolated behind provider and repository boundaries.
- Make testing, linting, formatting, and type checking part of the default workflow.

## Project Layout

```text
options_quant/
├── pyproject.toml
├── README.md
├── config/
│   └── examples/
├── notebooks/
├── tests/
└── src/options_quant/
    ├── config/
    ├── data/
    │   ├── providers/
    │   ├── models/
    │   └── repositories/
    ├── strategies/
    ├── backtest/
    ├── analytics/
    ├── reporting/
    └── utils/
```

## Architecture

This repository uses a src layout and follows clean architecture principles:

- Domain and strategy code should avoid direct dependencies on external services.
- Data providers adapt third-party APIs into internal models.
- Repositories define persistence/query boundaries.
- Backtesting, analytics, and reporting consume stable internal interfaces.
- Configuration is explicit and environment-specific.

Business logic has intentionally not been implemented yet.

## Development

Install dependencies:

```bash
poetry install
```

Run checks:

```bash
poetry run ruff check .
poetry run ruff format --check .
poetry run mypy .
poetry run pytest
```

Install pre-commit hooks:

```bash
poetry run pre-commit install
```

