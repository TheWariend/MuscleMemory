FROM node:24-alpine AS dashboard
WORKDIR /build
COPY package*.json ./
COPY apps/dashboard/package.json apps/dashboard/package.json
COPY packages/sdk-typescript/package.json packages/sdk-typescript/package.json
RUN npm ci
COPY apps/dashboard apps/dashboard
COPY packages/sdk-typescript packages/sdk-typescript
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.lock pyproject.toml LICENSE NOTICE ./
RUN pip install -r requirements.lock
COPY packages packages
COPY --from=dashboard --chown=10001:10001 /build/apps/dashboard/dist apps/dashboard/dist
RUN pip install --no-deps --no-build-isolation -e . && useradd --uid 10001 --create-home memory \
    && mkdir /models && chown 10001:10001 /models
USER memory
EXPOSE 8080
CMD ["musclememory", "serve", "--host", "0.0.0.0", "--port", "8080"]

