# Small image that runs one incremental sync and exits (Cloud Run Job, cron, Kubernetes Job).
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[mysql,postgres]"

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels \
    && useradd --create-home --uid 10001 sync
USER sync
WORKDIR /home/sync
ENTRYPOINT ["odata-sync-job"]
