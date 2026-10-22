# Build the SPA, then serve it from an unprivileged nginx (listens on 8080,
# runs as a non-root user) that also forwards /api to the BFF.
FROM node:26-alpine@sha256:0b36e8c136b94cd4fcf02188228e76c31ad5872eef3fec8cbd2eee500cfd9e80 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31-alpine@sha256:b9241c6e7b8e9a862f129d8d4199ab64b10390949a78bdd5603379b32c844083
# Alpine's pcre2, tiff, libexpat and libx11 fixes (image scan, PR-SEC-12.2; drop a package from the list once the base image carries the fix); the base runs as uid 101, so switch to root for the one command only.
USER root
RUN apk upgrade --no-cache pcre2 tiff libexpat libx11
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY security-headers.conf /etc/nginx/snippets/security-headers.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
