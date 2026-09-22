# CARE-data ETL 映像。由 CARE-infra 的 cicd build，在 care-vm 上以 care-etl
# CronJob 每天執行一次（ETL_RUN_ONCE=1）。
#
# 為什麼從 GitHub Actions 搬進叢集：向量要直接寫 care-vm 上的 pgvector，而 PG
# 的 Service 是 ClusterIP，叢集外連不到。理由見 vector_store.py。
# 版本對齊 .python-version（本機開發與測試用的版本）
FROM python:3.10-slim

# 與原 GitHub Actions workflow 用的 uv 版本一致
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# 先裝依賴再複製程式碼，改程式不必重裝套件
COPY pyproject.toml uv.lock ./
# --locked：uv.lock 與 pyproject.toml 不一致就讓 build 失敗，而非默默重解版本
RUN uv sync --locked --no-install-project

COPY . .

CMD ["uv", "run", "--no-sync", "python", "main_pipeline.py"]
