# TITAN

A personal AI assistant on your own machines. You chat with it from the
CLI; it keeps your tasks and what it learns about you, and keeps the data
of each user apart from the others.

Each user brings their own model: any OpenAI-compatible provider, with
their own key.

The project is an early prototype: a node starts, users sign in and set
their models, nothing more yet.

## Run a node

Set a database password once, then start the node:

```sh
echo "POSTGRES_PASSWORD=$(openssl rand -hex 32)" > .env
docker compose up -d
curl http://localhost:8000/health
```

The database keeps the password it was first started with; a new one in
`.env` takes effect only with a new database volume.

## Add a user

The admin adds a user on the node and gives them the one-time code it
prints:

```sh
docker compose exec app titan-admin add-user alice
```

The user signs in once from the CLI; later runs use the saved token:

```sh
titan login http://localhost:8000
titan whoami
```

## Set your models

Each user brings their own OpenAI-compatible models: for each, its URL,
its name and, unless it is a local one, a key. The key is typed without
echo. The first model added is the current one, which the chat uses:

```sh
titan settings model add
titan settings
titan settings model use <name>
titan settings model remove <name>
```

The node encrypts the keys with its node key, kept in the volume
`titan-node-key`, apart from the database. Back up both volumes: without
the node key, every user has to enter their key again.

## Licence

MIT, see [LICENSE](LICENSE).
