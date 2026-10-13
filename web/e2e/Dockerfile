# Playwright runner, pinned to the same version as the npm dependency so the
# bundled browsers match. It drives the web container over the compose network.
FROM mcr.microsoft.com/playwright:v1.63.0-noble

WORKDIR /e2e

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY playwright.config.ts ./
COPY specs ./specs

ENV CI=1
CMD ["npx", "playwright", "test", "specs/selfhost.spec.ts", "specs/screenshot.spec.ts", "specs/design.spec.ts"]
