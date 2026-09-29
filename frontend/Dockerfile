# syntax=docker/dockerfile:1.7
FROM node:22.17-alpine AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM dependencies AS build
ARG VITE_MAP_STYLE_URL=https://tiles.openfreemap.org/styles/liberty
ENV VITE_MAP_STYLE_URL=$VITE_MAP_STYLE_URL
COPY . .
RUN npm run build

FROM nginx:1.29-alpine AS runtime
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080

FROM dependencies AS tools
COPY . .
CMD ["npm", "run", "typecheck"]

FROM dependencies AS dev
COPY . .
EXPOSE 8080
CMD ["npm", "run", "dev", "--", "--port", "8080"]

FROM node:22.17-bookworm-slim AS e2e-node

# Add the pinned Node runtime to the official Chromium browser image.
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy AS e2e
COPY --from=e2e-node /usr/local/ /usr/local/
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY playwright.config.ts ./
COPY tests ./tests
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/ms-playwright/chromium-1148/chrome-linux/chrome
CMD ["npm", "run", "e2e"]
