# 社区版仅发布源码；固定官方提交，避免依赖已停止分发的服务端镜像。
FROM golang:1.24-bookworm@sha256:1a6d4452c65dea36aac2e2d606b01b4a029ec90cc1ae53890540ce6173ea77ac AS builder
ARG MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
ARG MC_COMMIT=d6541ea280b73a834b64d4097e21f2be77676104
WORKDIR /src
RUN git init minio && cd minio && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth 1 origin "$MINIO_COMMIT" && git checkout --detach FETCH_HEAD \
    && CGO_ENABLED=0 go build -trimpath -o /out/minio .
RUN git init mc && cd mc && git remote add origin https://github.com/minio/mc.git \
    && git fetch --depth 1 origin "$MC_COMMIT" && git checkout --detach FETCH_HEAD \
    && CGO_ENABLED=0 go build -trimpath -o /out/mc .

FROM alpine:3.22@sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8
COPY --from=builder /out/minio /out/mc /usr/local/bin/
COPY --from=builder /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/
COPY --from=builder /src/minio/LICENSE /usr/share/licenses/minio/LICENSE
COPY --from=builder /src/mc/LICENSE /usr/share/licenses/mc/LICENSE
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
ENV HOME=/tmp
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
