# WeatherData — GHCNh hourly-observations warehouse

Hourly observations for 112 US airport stations (ISD 2005–2024, GHCNh from 2025) loaded into Postgres,
feeding the barometric-pressure analytics (`obs_baro_impact`) that the bokeh apps read.
This repo holds ONLY the GHCNh pipeline; the Apple WeatherKit (awk*) Lambdas live elsewhere.

**This repo is public.** Never commit hosts, IPs, passwords, or anything from `.env`, `config.ini`,
`config.py`, or `aws/deploy/config.env` (all gitignored).

## Layout

- First-party code is ~12 files at the top level: `ghcnh_generate.py` (download), `ghcnh_process.py`
  (transform/load), `shared_funcs.py` (the single transform), `validate_precip.py`, `database_ddl.py`, ...
- `aws/ghcnh_download/` → Lambda `ghcnhDownloadS3`; `aws/ghcnh_postgresql_update/` → `ghcnhPostgresqlUpdate`.
  `aws/README.md` is the pipeline's runbook — read it before any AWS change.
- `env/` (the venv) and `aws/layer/build/` (vendored wheels + a *copy* of `shared_funcs.py`) are not
  first-party — don't search, lint, or edit them.
- `metadata/stations.csv` is the ISD↔GHCNh station roster. Bulk data dirs are gitignored.

## Environment

Venv is `env/` (Python 3.12.7) — use `env/bin/python`. `.python-version` says 3.13.2; the venv wins.
Lambda runtime and the `ghcnh_dependencies` layer are python3.12, manylinux2014_x86_64.
Local scripts read `.env` (`url_ext_aws` → EC2 Postgres via the SSH tunnel on localhost:15432);
"local" targets use `config.ini` via `shared_funcs.database_path()`.

## Commands

```bash
env/bin/python ghcnh_generate.py 2026
env/bin/python ghcnh_process.py 2026 7 local
env/bin/python validate_precip.py [local|remote] [first_year] [last_year]
./bash_scripts/checksum_yearly.sh 2026
```
Deploy (`aws/deploy/`, zsh): clean-account order is `04 → 00 → 01 → 02 → 03 → 05`.
**A change to `shared_funcs.py` reaches the Lambdas only by re-running `01` (layer) and `02` (functions).**
Manual run: `aws lambda invoke --function-name ghcnhPostgresqlUpdate --payload '{"periods": [[2025, 11]]}' out.json`.

## Verifying a change

No tests, CI, or ruff config. Use `validate_precip.py` for transform changes and `ruff check` on the files
you touched. The repo is not ruff-format clean — never bulk-format.

## Invariants and gotchas

- Downloader Lambda must stay OUTSIDE the VPC (no NAT; NOAA is reachable only publicly); loader must be INSIDE.
- Schedules are 07:30 / 07:45 America/Chicago; `load_window` uses `date.today()`, which is UTC on Lambda.
- Load guards (`aws/README.md`): min row/station counts, coverage must not shrink vs `manifest.json`,
  DELETE+COPY in one transaction, 3-day freshness check (`GHCNH_MAX_LAG_DAYS`). Don't weaken them to
  make a load pass.
- Lambda DB password comes from S3 at cold start (SSM is canonical but unreachable from the VPC). The layer
  has no `.env` and the Lambdas never call dotenv.
- Units: °F / inHg / mph / miles / inches. GHCNh is real units; ISD stored tenths. QC codes 2/3/6/7 → NULL.
  `prp` keeps only the 1-hour FM-15 value.
- Known data gap: 2025-08-30 to 2025-09-01.
- `database_ddl.py` uses `default=datetime.now()` (evaluated at import); `copy_observations` sets the
  timestamp explicitly — keep it that way.
- Rebuilding `obs_baro_impact` drops one-off grants; `sql/analytics_slp_decrease.sql` re-grants `mcp_ro`.
- Known stale bits: `README.md` still says deployment source is `~/Documents/aws/ghcnh_pipeline` (it is
  this repo), and `bash_scripts/run_psql.sh` has a broken shebang (`#/bin/zsh`).
