# Running on Google Cloud: Cloud Run Job + Cloud Scheduler

> **This is a guide.** The CI of this repository only builds the Docker image; it does not
> deploy anything and has no Google Cloud account configured. Replace every value in
> `UPPER_CASE` with your own and review costs and permissions before running.

The image runs one sync and exits, which is exactly what a **Cloud Run Job** is for. **Cloud
Scheduler** starts it on a schedule. The checkpoint lives either in the target database (Cloud
SQL) or in a JSON file on a **Cloud Storage** bucket mounted as a volume.

```text
Cloud Scheduler --(every 15 min)--> Cloud Run Job --(HTTPS)--> OData API
                                         |
                                         +--> Cloud SQL (MySQL or PostgreSQL): data + checkpoint
                                         +--> or Cloud Storage bucket: checkpoint.json
```

## 1. Variables

```bash
PROJECT=MY_PROJECT
REGION=southamerica-east1
REPO=odata-sync
IMAGE=$REGION-docker.pkg.dev/$PROJECT/$REPO/odata-incremental-sync:1.0.0
JOB=odata-sync-invoices
SA=odata-sync@$PROJECT.iam.gserviceaccount.com

gcloud config set project $PROJECT
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com sqladmin.googleapis.com
```

## 2. Build and push the image to Artifact Registry

```bash
gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION
gcloud builds submit --tag $IMAGE .
```

## 3. Service account and secrets

```bash
gcloud iam service-accounts create odata-sync --display-name="OData sync job"

# API token and database URL as secrets (never as plain env vars in the job definition)
printf '%s' 'THE_ODATA_TOKEN' | gcloud secrets create odata-token --data-file=-
printf '%s' 'postgresql://sync:DB_PASSWORD@10.20.0.3:5432/sync' | gcloud secrets create odata-target --data-file=-

for s in odata-token odata-target; do
  gcloud secrets add-iam-policy-binding $s \
    --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
done
```

## 4a. Checkpoint in Cloud SQL (recommended)

Keeping the checkpoint in the same database as the data is the simplest option
(`CHECKPOINT=target`, the default).

`TARGET` is a TCP URL (`host:port`), so give the Cloud SQL instance a **private IP** and let the
job reach the VPC with Direct VPC egress. The same works for MySQL and PostgreSQL. (The other
option is the Cloud SQL Auth Proxy as a sidecar container listening on `127.0.0.1`.)

```bash
gcloud run jobs create $JOB \
  --image=$IMAGE --region=$REGION --service-account=$SA \
  --network=MY_VPC --subnet=MY_SUBNET --vpc-egress=private-ranges-only \
  --set-env-vars=ODATA_URL=https://erp.exemplo.test/odata/,ODATA_ENTITY=Invoices,ODATA_KEY=Key,ODATA_CURSOR=ModifiedAt,ODATA_SERVER_TZ=America/Sao_Paulo,ODATA_MAX_TOP=100 \
  --set-secrets=ODATA_TOKEN=odata-token:latest,TARGET=odata-target:latest \
  --task-timeout=30m --max-retries=1
```

The `odata-target` secret then holds something like
`postgresql://sync:DB_PASSWORD@10.20.0.3:5432/sync` or `mysql://sync:DB_PASSWORD@10.20.0.3:3306/sync`
(URL-encode special characters in the password, e.g. `@` as `%40`).

## 4b. Checkpoint in a Cloud Storage bucket

Cloud Run can mount a bucket as a volume. `JsonFileCheckpoint` writes atomically (temporary file
plus rename), which works on the mounted bucket.

```bash
BUCKET=MY_PROJECT-odata-state
gcloud storage buckets create gs://$BUCKET --location=$REGION
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member=serviceAccount:$SA --role=roles/storage.objectAdmin

gcloud run jobs update $JOB --region=$REGION \
  --add-volume=name=state,type=cloud-storage,bucket=$BUCKET \
  --add-volume-mount=volume=state,mount-path=/mnt/state \
  --update-env-vars=CHECKPOINT=/mnt/state/invoices.json
```

Run only one execution at a time (`--parallelism=1`, the default for a single task) so two runs
never write the same checkpoint.

## 5. Run once and check the logs

```bash
gcloud run jobs execute $JOB --region=$REGION --wait
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=$JOB" \
  --limit=50 --format="value(textPayload)"
```

The last log line is a JSON summary: records, pages, checkpoint before and after, rows in the
target.

## 6. Schedule with Cloud Scheduler

```bash
gcloud iam service-accounts create odata-scheduler
gcloud run jobs add-iam-policy-binding $JOB --region=$REGION \
  --member=serviceAccount:odata-scheduler@$PROJECT.iam.gserviceaccount.com --role=roles/run.invoker

gcloud scheduler jobs create http $JOB-every-15-min \
  --location=$REGION --schedule="*/15 * * * *" --time-zone="America/Sao_Paulo" \
  --uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$JOB:run" \
  --http-method=POST \
  --oauth-service-account-email=odata-scheduler@$PROJECT.iam.gserviceaccount.com
```

## 7. Daily reconciliation

The incremental job keeps data fresh; a nightly reconciliation catches anything that slipped
through (rows backdated by the source system, records deleted locally by mistake). Wrap
`reconcile_by_keys` in a small script, build a second image or reuse this one with a different
entry point, and schedule it once a day for the last few days.

## Checklist

- [ ] `ODATA_SERVER_TZ` set to the zone the API uses for timestamps without offset.
- [ ] `ODATA_MAX_TOP` set if the API limits `$top`.
- [ ] Token and database URL only in Secret Manager.
- [ ] Target table with a primary key on the key field (or let the job create it).
- [ ] Alerts on failed executions (Cloud Monitoring, metric `run.googleapis.com/job/completed_execution_count` with result `failed`).
