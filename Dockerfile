FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder

ARG WORKDIR="/bot"

WORKDIR ${WORKDIR}
COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-install-project --no-dev

COPY . .

FROM python:3.13-slim-trixie

ARG  USER_ID="10000"
ARG  GROUP_ID="10001"
ARG  USER_NAME="Anat"
ARG  WORKDIR="/bot"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="${WORKDIR}/.venv/bin:$PATH" \
    PYTHONPATH="${WORKDIR}/.venv/lib/python3.13/site-packages"

WORKDIR ${WORKDIR}
COPY --from=builder ${WORKDIR} ${WORKDIR}
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg=* \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "${GROUP_ID}" "${USER_NAME}" && \
    useradd -l -u "${USER_ID}" -m "${USER_NAME}" -g "${USER_NAME}"

COPY --from=builder --chown=${USER_NAME}:${USER_NAME} ${WORKDIR} ${WORKDIR}

USER ${USER_ID}:${GROUP_ID}

CMD [ "python3", "main.py" ]
