# Project Rules for page-render-service

## Python Code Formatting & Quality
1. **Always format Python files before committing**: Whenever modifying Python files in `app/` or `tests/`, ensure imports are sorted (`I001`) and function signatures/formatting match `ruff format` output.
2. **Cyclomatic Complexity**: Keep function complexity low (`C901` <= 8). Split complex functions into top-level helpers.
3. **Pre-commit verification**: Run or verify formatting and linting rules before pushing to `main`.
