
FROM registry.access.redhat.com/ubi10/nodejs-24:latest AS build
WORKDIR /opt/app-root/src/frontend


COPY --chown=1001:0 frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY --chown=1001:0 frontend/ ./
RUN npm run build

FROM registry.access.redhat.com/ubi10/nginx-126:latest

COPY --from=build /opt/app-root/src/frontend/dist/ /opt/app-root/src/
COPY deploy/frontend-nginx.conf /opt/app-root/etc/nginx.default.d/spa.conf

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
