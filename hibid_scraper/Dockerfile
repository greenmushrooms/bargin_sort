# Build from the repository root, so the image carries both the scraper and the
# dbt project the flow runs after each scrape:
#
#   docker build -f hibid_scraper/Dockerfile -t integration-bargin-sort .

FROM prefecthq/prefect:3-python3.12

RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/prefect

# Dependencies first so edits to the source do not invalidate the pip layer.
COPY hibid_scraper/pyproject.toml hibid_scraper/README.md ./
RUN pip install --no-cache-dir .

# The scraper's modules sit at the workdir root, keeping the flow entrypoint
# `main.py:scrape_auctions` and its flat imports working unchanged.
COPY hibid_scraper/ ./

# find_dbt_project() looks here first.
COPY data__bargin_sort/ ./data__bargin_sort/
