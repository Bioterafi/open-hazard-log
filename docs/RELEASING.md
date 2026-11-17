# Releasing Open Hazard Log

Open Hazard Log keeps its own hazard log as a system in the tool, and every tagged release comes with an issued version of it. The issued version records the commit it assessed, so anyone can see which code the CSO signed off.

## Once: set up the tool's own log

Keep this database outside the repository, and back it up like any other hazard log.

```bash
mkdir -p ~/ohl-self/backups
uv run ohl --db ~/ohl-self/ohl.db init
uv run ohl --db ~/ohl-self/ohl.db user add you@example.org --name "Your Name" --role cso --registration "GMC 1234567"
uv run ohl --db ~/ohl-self/ohl.db seed-self --repo . --as you@example.org
uv run ohl --db ~/ohl-self/ohl.db serve --as you@example.org
```

The seed loads the hazards unrated. Rating them is the CSO's judgement. Each control that's in place names the test that shows it, and `--repo .` lets `ohl check` re-hash those test files on every run.

## Each release

1. Update the version in `pyproject.toml` and in `ohl/seed/ohl_self.toml` (the seed only matters for a fresh log), run `uv lock`, and commit all three files, `uv.lock` included: CI installs with `--locked` and fails if it's out of date. Set the same version on the system in the tool's own log:

   ```bash
   uv run ohl --db ~/ohl-self/ohl.db system configure --system 1 --version 0.1.1 --as you@example.org
   ```

2. CI is green on `main` for that commit: tests, lint, dependency audit, demo, and the wheel smoke test.

3. Add any new hazard this release introduces to the tool's own log before the work starts, not after.
4. Run the checks and decide every proposal they raise:

   ```bash
   uv run ohl --db ~/ohl-self/ohl.db check --system 1 --as you@example.org
   ```

5. With the release commit checked out and the working tree clean, issue a version. It records `git rev-parse HEAD` as the commit it assessed:

   ```bash
   uv run ohl --db ~/ohl-self/ohl.db issue --system 1 --note "Release 0.1.1" --confirm --as you@example.org
   ```

6. Publish the issued version and the audit trail next to the code, then tag:

   ```bash
   mkdir -p safety
   uv run ohl --db ~/ohl-self/ohl.db export --system 1 --version N --format json --out safety/ohl-hazard-log-vN.json
   uv run ohl --db ~/ohl-self/ohl.db audit --system 1 --out safety/ohl-audit.json
   uv run ohl verify --file safety/ohl-audit.json
   ```

   The tag points at the commit that adds these files; the issued version names the commit before it, which holds the code that was assessed.

7. Back up the tool's own log: `uv run ohl --db ~/ohl-self/ohl.db backup ~/ohl-self/backups/ohl-$(date +%F).db`.

## Not yet in place

- The named independent reviewer for changes to agents, prompts and safety configuration isn't appointed yet. Until it is, releases are approved by the CSO alone; say so in each issued version's note.
