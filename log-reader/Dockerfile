FROM golang:1.22-alpine AS builder
WORKDIR /src
COPY go.mod .
COPY main.go .
RUN go build -o log-reader .

FROM alpine:3.19
WORKDIR /app
COPY --from=builder /src/log-reader .
ENTRYPOINT ["./log-reader"]
