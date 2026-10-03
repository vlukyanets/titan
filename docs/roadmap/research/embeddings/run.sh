#!/bin/sh
# Runs bench.py against every candidate of ADR 0014, one TEI container at a
# time, on a node with the titan Compose project. Run from the project
# directory; results go to bench-results.jsonl. The node's own embeddings
# container is stopped meanwhile, so each candidate has the memory to itself.
# Arguments, if any, pick candidates by a part of their name.
set -u
IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.4
HERE=$(dirname "$0")
OUT=bench-results.jsonl
QWEN_QUERY='Instruct: Given a search query, retrieve notes and memories that match it
Query:'

PICK=$(echo "$@" | tr ' ' '|')
docker compose stop embeddings
docker compose exec -T worker mkdir -p /tmp/bench
for file in bench.py data.py; do
  docker compose cp "$HERE/$file" "worker:/tmp/bench/$file"
done

bench() { # model, query prefix, document prefix
  if [ -n "$PICK" ] && ! echo "$1" | grep -qE "$PICK"; then return; fi
  docker rm -f bench-tei >/dev/null 2>&1
  docker run -d --name bench-tei --network titan_default -v titan-bench-models:/data \
    "$IMAGE" --model-id "$1" --auto-truncate --max-batch-tokens 2048 >/dev/null
  tries=0
  until docker compose exec -T worker python -c \
    "import httpx; httpx.get('http://bench-tei/health', timeout=2).raise_for_status()" 2>/dev/null; do
    tries=$((tries + 1))
    if [ "$tries" -gt 360 ] || [ -z "$(docker ps -q -f name=bench-tei)" ]; then
      state=$(docker inspect -f 'exit {{.State.ExitCode}}, OOM killed {{.State.OOMKilled}}' bench-tei)
      echo "{\"model\": \"$1\", \"error\": \"did not start: $state\"}" >>"$OUT"
      docker logs --tail 5 bench-tei
      return
    fi
    sleep 5
  done
  result=$(docker compose exec -T -w /tmp/bench worker python bench.py http://bench-tei "$1" "$2" "$3")
  # Memory after indexing and the queries.
  memory=$(docker stats --no-stream --format '{{.MemUsage}}' bench-tei | cut -d/ -f1)
  echo "$result" | sed "s|}\$|, \"memory\": \"$memory\"}|" >>"$OUT"
  tail -1 "$OUT"
  docker rm -f bench-tei >/dev/null
}

bench intfloat/multilingual-e5-small "query: " "passage: "
bench intfloat/multilingual-e5-base "query: " "passage: "
bench Snowflake/snowflake-arctic-embed-m-v2.0 "query: " ""
bench Snowflake/snowflake-arctic-embed-l-v2.0 "query: " ""
bench BAAI/bge-m3 "" ""
bench Qwen/Qwen3-Embedding-0.6B "$QWEN_QUERY" ""

docker compose start embeddings
