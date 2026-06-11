FROM python:3.12-slim

# scp/ssh for the upload-to-car feature (mount your key into /root/.ssh)
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY raceline_studio ./raceline_studio
COPY maps ./maps
RUN uv pip install --system --no-cache .

EXPOSE 8754
ENTRYPOINT ["raceline-studio", "--host", "0.0.0.0", "--no-browser"]
CMD ["--map", "/app/maps/icra2026_map/map.yaml", "--out", "/racelines/icra2026_map_raceline.csv"]
