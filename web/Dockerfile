# Web container: builds the client, then serves it and proxies /api.
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM deps AS build
COPY index.html vite.config.js ./
COPY public ./public
COPY src ./src
RUN npx vite build

# Node tests, lint and the production build; run with `docker compose --profile test run web-test`.
FROM deps AS test
COPY index.html vite.config.js server.mjs ./
COPY public ./public
COPY src ./src
COPY tests ./tests
CMD ["npm", "run", "check"]

FROM node:22-slim AS runtime
ENV NODE_ENV=production
WORKDIR /app
COPY server.mjs ./
COPY --from=build /app/dist ./dist
USER node
EXPOSE 8080
CMD ["node", "server.mjs"]
