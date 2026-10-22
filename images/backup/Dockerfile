# Build and publish through the approved image pipeline, then commit its digest
# to backup.image.digest before unsuspending backups. No keys in this image.
FROM docker.io/library/postgres:16.15-alpine3.24@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea
USER root
RUN apk add --no-cache age bash \
    && command -v age && command -v bash && command -v pg_dump
USER 70:70
