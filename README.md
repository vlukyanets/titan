# TITAN

A personal AI assistant on your own machines. You chat with it from the
CLI; it keeps your tasks and what it learns about you, and keeps the data
of each user apart from the others.

Each user brings their own model: any OpenAI-compatible provider, with
their own key.

The project is an early prototype: a node starts and answers a health
check, nothing more yet.

## Run a node

Set a database password once, then start the node:

```sh
echo "POSTGRES_PASSWORD=$(openssl rand -hex 32)" > .env
docker compose up -d
curl http://localhost:8000/health
```

The database keeps the password it was first started with; a new one in
`.env` takes effect only with a new database volume.

## Licence

MIT, see [LICENSE](LICENSE).
