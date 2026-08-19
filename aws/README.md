# GHCNh pipeline on AWS

Nightly ELT that keeps the `weatherdata` warehouse current from NOAA's GHCNh
feed. Built to the same shape as the apple_weatherkit pipeline: an extract
Lambda that writes to S3, a load Lambda that reads S3 and writes Postgres, and
two EventBridge Scheduler entries fifteen minutes apart.

```
NOAA noaa-ghcnh-pds (us-east-1, public)
  │
  │  ghcnhDownloadS3          07:30 America/Chicago      NO VPC
  │    112 station parquets, 329 cols -> 32
  │    -> ghcnh_parquet_staging/<year>/data.parquet
  │    -> checked, then promoted
  ▼
s3://noaa-ghcnh-weatherdata/ghcnh_parquet/<year>/data.parquet  (+ manifest.json)
  │
  │  ghcnhPostgresqlUpdate    07:45 America/Chicago      IN VPC
  │    transform current + previous month
  │    DELETE window + COPY replacement in ONE transaction
  ▼
Postgres 16 on EC2 (private address, $PG_HOST in deploy/config.env)
  observations  ->  obs_baro_impact, loc_subset  ->  bokeh apps
```

## Why the two functions sit on different sides of the VPC

This is forced by the network, not a style choice.

`ghcnhDownloadS3` runs **outside** the VPC. `noaa-ghcnh-pds` is in **us-east-1**,
the VPC's only S3 route is a **us-east-2** gateway endpoint, and there is no NAT
gateway. Attached to the VPC it could not reach NOAA at all.

`ghcnhPostgresqlUpdate` runs **inside** the VPC, because the warehouse listens on
a private address. It reaches its own us-east-2 bucket over the gateway endpoint.

Same split as `awkApiCallS3` (no VPC) and `awkPostgresqlUpdate` (VPC).

## Why daily, and why two months

GHCNh republishes the whole in-progress year in place, once a day, with roughly a
two-day lag. Daily is exactly the rate at which new data appears.

Each run rebuilds the **current and previous month**. GHCNh keeps revising recent
observations, and a current-month-only job stops looking at the previous month
the moment the month rolls over — so a correction published on the 2nd for the
30th would never be collected. The extra month costs a few seconds of COPY.

In January the window spans two years, so the download step fetches two years'
files and the loader reads December from one and January from the other.

## Guards

Both halves refuse rather than write bad data. This is not theoretical: on
2026-08-19, mid-build, NOAA republished the 2026 files ending at **2026-07-15**
having ended at **2026-08-16** the day before — a month of observations withdrawn
upstream, with row count and station count both still looking healthy.

| Guard | Where | Catches |
|---|---|---|
| min raw rows, min stations | download, before promote | truncated or failed download |
| **coverage must not regress** | download, before promote | upstream withdrawing data — the case above |
| min window rows | loader, **before the DELETE** | short source emptying a good window |
| DELETE + COPY in one transaction | loader | readers seeing the window half-loaded; a failed load leaving a hole |
| freshness (newest row within 3 days) | loader, after load | upstream that has stopped publishing |

The coverage-regression guard compares against `manifest.json`, a small sidecar
written next to each promoted parquet, so the check costs one small GET rather
than re-parsing 40 MB.

To override deliberately, when NOAA really has withdrawn data and you want the
shorter file:

```bash
aws lambda invoke --function-name ghcnhDownloadS3 \
  --payload '{"years": [2026], "allow_regression": true}' out.json
```

## Layout

```
ghcnh_download/lambda_function.py            extract  (mirrors ghcnh_generate.py)
ghcnh_postgresql_update/lambda_function.py   load     (mirrors ghcnh_process.py)
layer/requirements.txt                       pyarrow, SQLAlchemy, psycopg2
deploy/00_bootstrap_aws.sh                   bucket, SG, IAM roles, SSM parameter
deploy/01_build_layer.sh                     build + publish the layer
deploy/02_deploy_functions.sh                package + deploy both functions
deploy/03_create_schedules.sh                the two daily schedules
deploy/04_postgres_role.sh                   ghcnh_etl role + pg_hba on EC2
deploy/05_create_alarms.sh                   SNS topic + CloudWatch alarms
deploy/config.env.example                    template for the values below
```

## Before deploying anything

Every script reads its identifiers from `deploy/config.env`, which is
**gitignored** and which you have to create:

```bash
cd aws/deploy
cp config.env.example config.env
$EDITOR config.env          # account id, EC2 host, VPC, subnets, key path
```

This repository is public. None of those values is a credential, but together
they describe an AWS account and an EC2 host reachable from the internet on a
security group that permits SSH from `0.0.0.0/0` — which is a map worth not
publishing. Scripts refuse to run if the file is missing.

Run order on a clean account: `04` → `00` → `01` → `02` → `03` → `05`.

## The transform lives in the repo, not here

`01_build_layer.sh` copies `shared_funcs.py` out of
`~/projects/WeatherData` into the layer. The Lambdas run the same transform,
converters, quality-code rejects and COPY writer that `python ghcnh_process.py`
runs on the laptop — there is no second implementation to drift. The loader
likewise runs the repo's own `sql/analytics_slp_decrease.sql` and
`sql/extend_loc_subset.sql`, copied into its package at deploy time.

**A fix to the transform reaches the pipeline by re-running `01` and `02`.**

## The layer

pyarrow is why a layer exists, and it nearly does not fit: 230 MB installed
against a 250 MB hard limit for all layers plus function code. The build trims
tests, headers, `dist-info` and Arrow Flight down to 165 MB.

Judge what is removable by the **ELF `NEEDED` graph**, not by what Python
imports. `libarrow_substrait.so` looks unreachable — nothing imports
`pyarrow.substrait` — but the wheel links it into `lib`, `_compute`, `_parquet`,
`_dataset` and fifteen others, and removing it breaks `import pyarrow` outright.
Arrow Flight is genuinely isolated and is the one safe 20 MB.

```bash
readelf -d layer/build/python/pyarrow/*.so* | grep NEEDED   # after a pyarrow bump
```

## The warehouse password

The loader reads it from `s3://$GHCNH_BUCKET/secrets/ghcnh_etl_password`
at cold start, and the canonical copy lives in SSM Parameter Store at
`/weatherdata/ghcnh_etl/password`.

SSM at runtime was the first design and this VPC cannot support it: no NAT
gateway, and the only endpoint is the S3 *gateway* endpoint, so
`ssm.us-east-2.amazonaws.com` is unreachable — the loader hung until it timed
out. An *interface* endpoint would fix it for about $21/month across these three
subnets, to hold one 32-character string.

A Lambda environment variable — what `awkPostgresqlUpdate` does — is free but
readable by anyone who can call `GetFunctionConfiguration`. The bucket costs
nothing, keeps the value out of the function's configuration, and restricts it
to this one IAM role; the bucket has default encryption and blocks public access.

To rotate: change it in SSM and on the EC2 role, then re-run `02`.

## Cost

About **$0.02/month**. Two invocations a day, ~10 s and ~30 s at 2048 MB, plus
a few hundred MB of S3. The EC2 instance and its Postgres were already running.

## Operating it

```bash
# run either half by hand
aws lambda invoke --function-name ghcnhDownloadS3 --payload '{}' out.json
aws lambda invoke --function-name ghcnhPostgresqlUpdate --payload '{}' out.json

# backfill a specific window
aws lambda invoke --function-name ghcnhDownloadS3 \
  --payload '{"years": [2025]}' out.json
aws lambda invoke --function-name ghcnhPostgresqlUpdate \
  --payload '{"periods": [[2025, 11], [2025, 12]]}' out.json

# logs
aws logs tail /aws/lambda/ghcnhPostgresqlUpdate --since 1d

# pause the pipeline
aws scheduler update-schedule --name ghcnhDownloadS3 --state DISABLED ...
```

## Alarms

A failure here is quiet by design. Every guard refuses to write rather than
writing something wrong, so a broken run leaves the warehouse serving perfectly
good *stale* data — the bokeh apps keep plotting and nothing downstream looks
different. The only symptom is that `max(date)` stops advancing.

Three alarms publish to the `ghcnh-pipeline-alerts` SNS topic:

| Alarm | Fires when |
|---|---|
| `ghcnh-ghcnhDownloadS3-errors` | the extract raised — NOAA unreachable, or the promote guard refused |
| `ghcnh-ghcnhPostgresqlUpdate-errors` | the load raised — short source, stale source, warehouse unreachable |
| `ghcnh-pipeline-idle` | the loader has not run in 24h — schedule disabled or its IAM role broken |

The idle alarm is the one that catches what the others cannot: if the schedule
stops firing, nothing runs, so nothing errors, and an errors alarm stays green
forever.

Email subscriptions must be confirmed from the message SNS sends, or alarms fire
into nothing. Check with:

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn "arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:ghcnh-pipeline-alerts" \
  --query 'Subscriptions[].[Endpoint,SubscriptionArn]' --output table
```

A `SubscriptionArn` of `PendingConfirmation` means the link has not been clicked.
