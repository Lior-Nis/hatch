# Deploying Hatch on one server

Hatch runs as three containers: Postgres, the dashboard (`web`) and the background `worker`.

    git clone <repo> /opt/hatch && cd /opt/hatch
    cp .env.production.example .env && chmod 600 .env     # set POSTGRES_PASSWORD, then optional keys
    docker compose -f docker-compose.prod.yml up -d --build
    docker compose -f docker-compose.prod.yml exec web hatch seed-ips
    docker compose -f docker-compose.prod.yml exec web hatch seed-models

The dashboard has no login of its own and can approve videos. It listens on 127.0.0.1:18321
only. Put it behind a reverse proxy that enforces authentication, and use HTTPS when a domain exists.

Keys go in the server's `.env`, never in the repository or in chat:
`HF_KEY`, `HATCH_ANTHROPIC_API_KEY`, `HATCH_BUFFER_API_KEY`. Restart with
`docker compose -f docker-compose.prod.yml up -d` after editing. The default
`HATCH_MEDIA_PROVIDER=fake` generates free synthetic videos; real generation costs money and
is bounded by the budget limits.
