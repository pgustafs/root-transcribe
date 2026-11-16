# Frontend image on Red Hat Hardened Images (Project Hummingbird,
# https://catalog.hummingbird-project.io). Both images are distroless at
# runtime, so everything that needs a shell happens in the -builder stage and
# the runtime stage only copies files in (no RUN there).
#
# For releases, pin both images by digest (…:26-builder@sha256:…) and verify
# the Red Hat signature; scan with Grype (Trivy does not detect this OS).
ARG NODEJS_IMAGE=registry.access.redhat.com/hi/nodejs:26-builder
ARG NGINX_IMAGE=registry.access.redhat.com/hi/nginx:1.30

FROM ${NODEJS_IMAGE} AS build
USER 0
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build && chmod -R a+rX,go-w dist

FROM ${NGINX_IMAGE} AS runtime
COPY --from=build /build/dist/ /usr/share/nginx/html/
COPY deploy/frontend-nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
# User (65532), entrypoint and command come from the base image:
# nginx -c /etc/nginx/nginx.conf -g 'daemon off;'
