FROM python:3.12-alpine
ARG SOURCE_COMMIT=unknown
ENV RELEASE_SHA=${SOURCE_COMMIT}
WORKDIR /app
COPY app.py .
RUN adduser -D relay && mkdir -p /data && chown relay:relay /data
USER relay
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/health || exit 1
CMD ["python", "-u", "app.py"]
