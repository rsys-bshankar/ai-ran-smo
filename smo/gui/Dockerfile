# Build the SPA, then serve it from an unprivileged nginx (listens on 8080,
# runs as a non-root user) that also forwards /api to the BFF.
FROM node:26-alpine@sha256:40cd011b55415bc894e45b02fdd5a5dfde056921b7d23a64c74f80497af48797 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31-alpine@sha256:b5047b9b76845eac124358373c6852377c52e4b05834a47647e955dd91cb8f1e
# Alpine's pcre2, tiff, libexpat, libx11, brotli-libs (CVE-2026-111497) and curl / libcurl (CVE-2026-92392) fixes (image scan, PR-SEC-12.2; drop a
# package from the list once the base image carries the fix); the base runs as uid 101, so switch to root for the one command only.
USER root
RUN apk upgrade --no-cache pcre2 tiff libexpat libx11 brotli-libs curl libcurl
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
