# Contributing

Bug reports and pull requests are welcome.

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Keep the runtime dependency list short: `requests` (and `tzdata` for time zones). Database
   drivers stay optional extras.
3. Add tests for every change, including the error paths. Prefer the fake server in
   `examples/fake_server.py` over mocks when the behavior depends on the server.
4. Use made-up data only (`exemplo.test`, fictional invoices). Never commit real endpoints,
   tokens or records.
5. Before pushing:

   ```bash
   pip install -e ".[dev]"
   ruff check . && ruff format --check . && pytest
   ```

   The MySQL and PostgreSQL tests in `tests/e2e` run in CI; locally they are skipped unless
   `MYSQL_HOST` / `POSTGRES_DSN` are set.

6. Write commit messages in the imperative ("Add composite key support"), one topic per commit.

By contributing you agree that your work is released under the MIT license.
