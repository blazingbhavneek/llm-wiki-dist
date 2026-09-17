# Elasticsearch for GROWI

This repository includes a single-node Elasticsearch 9.3.3 service for GROWI
full-text search. It installs the ICU and Kuromoji analysis plugins from local
ZIP files, so startup does not depend on internet access.

## Start the bundled stack

Requirements:

- Docker with the Compose plugin
- Enough free disk space for Elasticsearch data
- Port 3000 available for GROWI

From the repository root, run:

```bash
docker compose -f growi-stack/docker-compose.yml up -d
docker compose -f growi-stack/docker-compose.yml ps
```

The expected services are `app`, `mongo`, `elasticsearch`, and
`pdf-converter`. Elasticsearch and MongoDB should report `healthy`. Open
GROWI at <http://localhost:3000> and complete its setup wizard on the first
run.

Elasticsearch data is kept in the `es_data` Docker volume. Normal container
recreation and `docker compose down` preserve it. Do not use `down -v` unless
you intentionally want to delete the search index and other stack data.

## Connect GROWI to Elasticsearch

GROWI reads its Elasticsearch endpoint from `ELASTICSEARCH_URI`. The bundled
services share a Compose network, so GROWI connects with the Elasticsearch
service name rather than `localhost`:

```yaml
services:
  app:
    environment:
      ELASTICSEARCH_URI: http://elasticsearch:9200/growi
```

The final `/growi` is the index name. This setting is already present in
`growi-stack/docker-compose.yml`; starting the stack connects GROWI
automatically.

For an existing GROWI container, put GROWI and Elasticsearch on the same
Docker network and set the same variable:

```yaml
services:
  growi:
    environment:
      ELASTICSEARCH_URI: http://elasticsearch:9200/growi
    networks: [growi]

  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:9.3.3
    networks: [growi]

networks:
  growi:
```

If the services are in separate Compose projects, attach both services to a
shared external Docker network. `localhost` inside the GROWI container points
back to GROWI itself, not Elasticsearch.

After changing `ELASTICSEARCH_URI`, recreate GROWI:

```bash
docker compose -f growi-stack/docker-compose.yml up -d --force-recreate app
```

For an existing wiki, use GROWI's administration screen to rebuild its search
index so pages created before Elasticsearch was connected become searchable.

## Configuration in this repository

The Elasticsearch service mounts:

- `growi-stack/elasticsearch/v9/config/elasticsearch.yml` for server settings
- `growi-stack/elasticsearch/v9/config/elasticsearch-plugins.yml` for plugins
- `growi-stack/elasticsearch/v9/plugins/` for offline plugin ZIP files
- the `es_data` volume for persistent indices

Plugin versions must match the Elasticsearch image version. Elasticsearch 9
plugin declarations use `location`, not `url`:

```yaml
plugins:
  - id: analysis-kuromoji
    location: file:/opt/plugins/analysis-kuromoji-9.3.3.zip
  - id: analysis-icu
    location: file:/opt/plugins/analysis-icu-9.3.3.zip
```

Security is disabled for this local stack, and port 9200 is not published to
the host. Do not expose this Elasticsearch service directly to an untrusted
network.

## Verify the connection

Check the containers, Elasticsearch API, and installed plugins:

```bash
docker compose -f growi-stack/docker-compose.yml ps
docker compose -f growi-stack/docker-compose.yml exec -T elasticsearch \
  curl -fsS http://localhost:9200/_cluster/health
docker compose -f growi-stack/docker-compose.yml exec -T elasticsearch \
  curl -fsS 'http://localhost:9200/_cat/plugins?v'
docker compose -f growi-stack/docker-compose.yml logs --tail=100 app elasticsearch
```

A single-node cluster normally reports `yellow`: primary shards are active,
but replicas cannot be placed on a second node. `red` means a primary shard
is unavailable and must be investigated.

## Troubleshooting

### Elasticsearch is unhealthy and logs say `unknown field [url]`

Use `location` in `elasticsearch-plugins.yml`. The `url` field is not valid in
the Elasticsearch 9 plugin configuration schema.

### Cluster health is red and shards are unassigned

Ask Elasticsearch why allocation was rejected:

```bash
docker compose -f growi-stack/docker-compose.yml exec -T elasticsearch \
  curl -fsS -X POST http://localhost:9200/_cluster/allocation/explain
```

This development stack uses absolute disk watermarks because a large host
filesystem can exceed Elasticsearch's percentage watermark while still
having tens of gigabytes free. Adjust the values in `elasticsearch.yml` to
match the host's capacity; do not disable disk protection.

### GROWI cannot connect

Confirm that both containers share a network and that the URI uses
`elasticsearch`, not `localhost`. Then inspect both logs:

```bash
docker compose -f growi-stack/docker-compose.yml logs --tail=100 app elasticsearch
```

